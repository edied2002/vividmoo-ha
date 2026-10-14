"""Data coordinator: REST polling plus MQTT pushes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import random
import ssl
import time
from typing import Any
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.ssl import client_context

from .api import VividmooApi, VividmooAuthError, VividmooError, VividmooTokenRevoked
from .const import (
    CONF_ENABLE_MQTT,
    CONF_MQTT_VERIFY_TLS,
    CONTACT_ONLINE,
    DEFAULT_ENABLE_MQTT,
    COMMAND_ACK_TIMEOUT,
    DOMAIN,
    MQTT_KICK_BACKOFF,
    UPDATE_INTERVAL,
    VISIT_BACKFILL_MAX_PAGES,
    VISIT_HISTORY,
    WORK_MODE_IDLE,
    WORK_STATE_RUNNING,
)
from .mqtt import VividmooMqtt
from .protocol import risp

_LOGGER = logging.getLogger(__name__)


@dataclass
class BoxState:
    serial: str
    info: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    use_times: dict[str, Any] = field(default_factory=dict)
    last_record: dict[str, Any] | None = None
    online: bool | None = None
    im_user_id: str | None = None
    # From MQTT pushes (d2c1020 / d2c1021 / d2c1023).
    work_mode: int | None = None
    work_state: int | None = None
    work_cause: int | None = None
    all_state: dict[str, int] = field(default_factory=dict)
    last_message: dict[str, int] | None = None
    # Cat visit records (messageType 0) by recordId, kept for VISIT_HISTORY.
    visits: dict[int, dict[str, Any]] = field(default_factory=dict)
    backfilled: bool = False
    # Job timing from state pushes (box epoch seconds). The box streams nothing during a job
    # (verified in two captures), so elapsed time = run_accumulated + (now - running_since).
    job_mode: int | None = None
    job_timing_known: bool = False
    job_started: float | None = None
    run_accumulated: float = 0.0
    running_since: float | None = None


class VividmooCoordinator(DataUpdateCoordinator[dict[str, BoxState]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: VividmooApi) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.entry = entry
        self.api = api
        self._mqtt: VividmooMqtt | None = None
        self._im_to_serial: dict[str, str] = {}
        self._mqtt_retry_at = 0.0
        self._seq = random.randint(1000, 30000)
        # seq -> (expected cmd, future). The box sends its replies to every linked account (observed live:
        # we received the official app's 1021 reply), so match on cmd as well as seq.
        self._pending: dict[int, tuple[int, asyncio.Future[risp.RispFrame]]] = {}
        self._command_lock = asyncio.Lock()

    @property
    def live_updates(self) -> bool:
        """True while the MQTT session is connected, so pushed state (e.g. cat presence) is current."""
        return self._mqtt is not None and bool(self._mqtt.connected)

    async def _async_update_data(self) -> dict[str, BoxState]:
        try:
            return await self._refresh()
        except VividmooAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except VividmooTokenRevoked as err:
            # Another login to the account took our token; not a credentials problem.
            raise UpdateFailed(str(err)) from err
        except VividmooError as err:
            raise UpdateFailed(str(err)) from err

    async def _refresh(self) -> dict[str, BoxState]:
        previous = self.data or {}
        boxes: dict[str, BoxState] = {}
        for info in await self.api.devices():
            serial = info.get("serialNumber")
            if not serial:
                continue
            box = previous.get(serial) or BoxState(serial)
            box.info = info
            box.config = await self._optional(self.api.box_config(serial), box.config)
            box.use_times = await self._optional(self.api.box_use_times(serial), box.use_times)
            records = await self._optional(self._visit_records(box), None)
            if records:
                box.last_record = records[0]
            boxes[serial] = box

        contacts = await self._optional(self.api.im_contacts(), [])
        for contact in contacts or []:
            box = boxes.get(contact.get("userName"))
            if box is not None:
                box.im_user_id = str(contact.get("userId"))
                box.online = contact.get("onlineStatus") == CONTACT_ONLINE
                self._im_to_serial[box.im_user_id] = box.serial

        if (
            self.entry.options.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT)
            and (self._mqtt is None or not self._mqtt.connected)
            and time.monotonic() >= self._mqtt_retry_at
        ):
            await self._start_mqtt()
        return boxes

    async def _visit_records(self, box: BoxState) -> list[dict[str, Any]]:
        """Newest page of visits, plus a one-off cursor backfill until VISIT_HISTORY is covered."""
        records = await self.api.records(box.serial, size=10)
        _merge_visits(box, records)
        cutoff = _now_ms() - VISIT_HISTORY.total_seconds() * 1000
        if not box.backfilled:
            for _ in range(VISIT_BACKFILL_MAX_PAGES):
                oldest = min((r["eventTime"] for r in box.visits.values()), default=None)
                if oldest is None or oldest <= cutoff:
                    break
                older = await self.api.records(box.serial, size=10, before=oldest)
                if not older:
                    break
                _merge_visits(box, older)
            box.backfilled = True  # afterwards the newest page (~30 records, ~6 days) keeps it current
        box.visits = {k: v for k, v in box.visits.items() if v["eventTime"] >= cutoff}
        return records

    async def _optional(self, coro, default):
        """Secondary endpoints must not take the whole device down."""
        try:
            return await coro
        except (VividmooAuthError, VividmooTokenRevoked):
            raise
        except VividmooError as err:
            _LOGGER.debug("Optional call failed: %s", err)
            return default

    async def _start_mqtt(self) -> None:
        if self._mqtt is not None:
            await self.hass.async_add_executor_job(self._mqtt.stop)
            self._mqtt = None
        try:
            creds = await self.api.im_login()
        except VividmooError as err:
            _LOGGER.warning("IM login failed, MQTT pushes disabled this cycle: %s", err)
            return
        verify = self.entry.options.get(CONF_MQTT_VERIFY_TLS, True)
        ctx = client_context() if verify else await self.hass.async_add_executor_job(_insecure_context)
        host = urlparse(self.api.base_url).hostname
        self._mqtt = VividmooMqtt(
            self.hass, host, creds, ctx, self._handle_frame, self._handle_mqtt_lost, self._handle_mqtt_connected
        )
        await self.hass.async_add_executor_job(self._mqtt.start)

    async def async_shutdown(self) -> None:
        if self._mqtt is not None:
            await self.hass.async_add_executor_job(self._mqtt.stop)
            self._mqtt = None
        await super().async_shutdown()

    @callback
    def _handle_mqtt_lost(self, reason: str) -> None:
        """The account allows one IM session per client type; the official app wins.

        Re-logging in on every refresh would kick the app's live connection in turn,
        so back off instead of fighting over the session.
        """
        self._mqtt_retry_at = time.monotonic() + MQTT_KICK_BACKOFF.total_seconds()
        _LOGGER.warning(
            "Vividmoo live updates stopped (%s); retrying in %s. REST polling continues",
            reason,
            MQTT_KICK_BACKOFF,
        )
        if self._mqtt is not None:
            mqtt, self._mqtt = self._mqtt, None
            self.hass.async_add_executor_job(mqtt.stop)
        for _, future in self._pending.values():
            if not future.done():
                future.set_exception(HomeAssistantError(f"Cloud session lost: {reason}"))
        self.async_update_listeners()  # push-only entities (cat inside) go unavailable

    # -- commands ---------------------------------------------------------------

    async def async_reset_use_times(self, serial: str) -> None:
        """Tell the cloud the waste bin was emptied (resets the use counter), then refresh the counts."""
        try:
            await self.api.reset_box_use_times(serial)
        except VividmooError as err:
            raise HomeAssistantError(f"Resetting the waste bin counter failed: {err}") from err
        await self.async_request_refresh()

    async def async_operate(self, serial: str, op_mode: int, op_state: int) -> None:
        """Send d2c1019 (start/pause/resume a clean, smooth or empty) and wait for the box's ack."""
        await self._async_command(serial, risp.CMD_OPERATE, lambda seq: risp.build_operate_command(seq, op_mode, op_state))

    async def async_request_state(self, serial: str) -> BoxState:
        """Ask the box for its full state (d2c1021); the reply is applied by _handle_frame."""
        await self._async_command(serial, risp.CMD_ALL_STATE, risp.build_state_request_command)
        return self.data[serial]

    async def _async_command(self, serial: str, cmd: int, build) -> risp.RispFrame:
        box = (self.data or {}).get(serial)
        if box is None or not box.im_user_id:
            raise HomeAssistantError("The litter box's cloud identity is unknown (is it online?)")
        async with self._command_lock:
            opened_here = await self._ensure_session()
            try:
                self._seq = self._seq % 0xFFFF + 1
                seq = self._seq
                future: asyncio.Future[risp.RispFrame] = self.hass.loop.create_future()
                self._pending[seq] = (cmd, future)
                payload = risp.encode_envelope(self._mqtt.uid, box.im_user_id, build(seq))
                try:
                    _LOGGER.debug("Sending RISP dev=2 cmd=%s seq=%s to %s", cmd, seq, box.im_user_id)
                    await self.hass.async_add_executor_job(self._mqtt.publish, payload)
                    ack = await asyncio.wait_for(future, COMMAND_ACK_TIMEOUT)
                    _LOGGER.debug("Box acknowledged cmd=%s seq=%s", cmd, seq)
                    return ack
                except (ConnectionError, TimeoutError) as err:
                    raise HomeAssistantError(f"The litter box did not acknowledge the command ({err!r})") from err
                finally:
                    self._pending.pop(seq, None)
            finally:
                if opened_here and not self.entry.options.get(CONF_ENABLE_MQTT, DEFAULT_ENABLE_MQTT) and self._mqtt:
                    mqtt, self._mqtt = self._mqtt, None
                    await self.hass.async_add_executor_job(mqtt.stop)

    async def _ensure_session(self) -> bool:
        """Make sure the IM/MQTT session is up; return True if this call opened it."""
        if self._mqtt is not None and self._mqtt.connected:
            return False
        remaining = self._mqtt_retry_at - time.monotonic()
        if remaining > 0:
            raise HomeAssistantError(
                f"Cloud session was taken over by another login; commands paused for {remaining / 60:.0f} min "
                "(use a separate account shared to the box)"
            )
        await self._start_mqtt()
        if self._mqtt is None:
            raise HomeAssistantError("Could not open the cloud session")
        try:
            await asyncio.wait_for(self._mqtt.connected_event.wait(), COMMAND_ACK_TIMEOUT)
        except TimeoutError as err:
            raise HomeAssistantError("Timed out connecting to the cloud session") from err
        return True

    @callback
    def _handle_mqtt_connected(self) -> None:
        """On every (re)connect, ask each box for its full state so mode/state are never unknown."""
        self.entry.async_create_background_task(self.hass, self._async_sync_states(), f"{DOMAIN} sync state")

    async def _async_sync_states(self) -> None:
        for serial, box in (self.data or {}).items():
            if not box.im_user_id:
                continue
            try:
                await self.async_request_state(serial)
            except HomeAssistantError as err:
                _LOGGER.debug("Initial state request for %s failed: %s", serial, err)

    @callback
    def _handle_frame(self, env: risp.ImEnvelope, frame: risp.RispFrame) -> None:
        pending = self._pending.get(frame.seq) if frame.attr == risp.Attr.REQACK else None
        if pending and pending[0] == frame.cmd and not pending[1].done():
            pending[1].set_result(frame)  # matched by seq (as the app's lf/a does) and by cmd
        serial = self._im_to_serial.get(env.sender)
        box = (self.data or {}).get(serial) if serial else None
        if box is None:
            _LOGGER.debug("RISP frame from unknown sender %s (dev=%s cmd=%s)", env.sender, frame.dev, frame.cmd)
            return
        try:
            msg = risp.decode_payload_message(frame)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not parse payload for %s", frame.key)
            return
        if msg is None:
            _LOGGER.debug("Unmapped RISP %s attr=%s payload=%s", frame.key, frame.attr, frame.payload.hex())
            return
        # Mirrors the official app's dispatcher (xb/w.c): the state push is cmd 1024 with the
        # full all-state payload, the same message as the 1021 reply. 1020 and 2534 pushes are
        # ignored by the app; a partial 1020 (proto3 drops w_mode=0) would read as "idle".
        if frame.key in ((2, 1021), (2, 1024)):
            _track_job(box, int(msg.w_mode), int(msg.w_state), float(msg.timestamp), live_push=frame.key == (2, 1024))
            box.work_mode, box.work_state, box.work_cause = int(msg.w_mode), int(msg.w_state), msg.w_cause
            box.all_state = {f.name: int(v) for f, v in msg.ListFields()}
        elif frame.key == (2, 1023):
            box.last_message = {"timestamp": msg.timestamp, "msg": msg.msg, "msg_type": msg.msg_type}
        else:
            _LOGGER.debug("Ignoring RISP %s (not used by the official app): %s", frame.key, msg)
            return
        self.async_set_updated_data(self.data)


def _track_job(box: BoxState, mode: int, state: int, ts: float, live_push: bool) -> None:
    """Follow one job's run time across start/pause/resume using the box's own timestamps."""
    if mode == WORK_MODE_IDLE:
        box.job_mode, box.job_timing_known, box.job_started = None, False, None
        box.run_accumulated, box.running_since = 0.0, None
        return
    running = state == WORK_STATE_RUNNING
    if box.job_mode != mode:
        # A new job. Only a live 1024 push marks its true start; a 1021 snapshot (e.g. after a
        # restart mid-job) gives the mode but not how long it has been running.
        box.job_mode, box.job_timing_known = mode, live_push and running
        box.job_started = ts if box.job_timing_known else None
        box.run_accumulated, box.running_since = 0.0, (ts if running else None)
        return
    if running and box.running_since is None:
        box.running_since = ts  # resumed
    elif not running and box.running_since is not None:
        box.run_accumulated += max(0.0, ts - box.running_since)  # paused / blocked
        box.running_since = None


def _now_ms() -> int:
    return int(dt_util.utcnow().timestamp() * 1000)


def _merge_visits(box: BoxState, records: list[dict[str, Any]]) -> None:
    for record in records:
        if isinstance(record.get("recordId"), int) and isinstance(record.get("eventTime"), int):
            box.visits[record["recordId"]] = record


def _insecure_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx
