"""Home Assistant-level tests with the cloud API mocked."""

from unittest.mock import patch

import pytest
import voluptuous as vol

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries  # noqa: E402
from homeassistant.const import STATE_OFF, STATE_ON  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.vividmoo.api import MqttCredentials, VividmooAuthError, VividmooTokenRevoked  # noqa: E402
from custom_components.vividmoo.const import DOMAIN  # noqa: E402
from custom_components.vividmoo.protocol import messages, risp  # noqa: E402

ENTRY_DATA = {
    "account": "me@example.com",
    "password": "secret",
    "app_id": "APPID",
    "app_key": "KEY",
    "account_type": 1,
    "password_mode": "plain",
    "base_url": "https://apis-eu.airrobo-home.com",
    "client_device_id": "0123456789abcdef",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def mock_api():
    with patch("custom_components.vividmoo.api.VividmooApi", autospec=True) as cls:
        api = cls.return_value
        api.base_url = ENTRY_DATA["base_url"]
        # Shapes (and integer types) copied from live responses on 2026-10-11, values changed.
        api.devices.return_value = [{"serialNumber": "SN1", "deviceName": "Kitchen box", "deviceStatus": 1,
                                     "shareFlag": 0, "adminUserNickName": None, "boxColor": 0, "defaultFlag": 1}]
        api.box_config.return_value = {"wifiName": "home", "weighUnit": 0, "boxUseTimes": 501, "boxFunctionVOList": []}
        api.box_use_times.return_value = {"boxUseTimes": 501, "boxFullMax": 50, "boxFullAlert": 25, "percentage": 100,
                                          "alertType": None, "boxUseTimesResetTime": None}
        api.records.return_value = [{"recordId": 1, "eventTime": 1_791_732_718_000, "catNickname": "Moo", "catInfoId": 6,
                                     "eventType": 3, "confirmed": 1, "weight": 5.9, "costTime": 36,
                                     "timeStr": "2026-10-11 23:31:58", "serialNumber": "SN1"}]
        api.im_contacts.return_value = [{"userName": "SN1", "userId": 555, "onlineStatus": 1}]
        api.im_login.return_value = MqttCredentials("cid", "u", "p", "pub", "sub", "1")
        with (
            patch("custom_components.vividmoo.build_api", return_value=api),
            patch("custom_components.vividmoo.config_flow.build_api", return_value=api),
        ):
            yield api


@pytest.fixture
def mock_mqtt():
    with patch("custom_components.vividmoo.coordinator.VividmooMqtt") as cls:
        cls.return_value.connected = True
        yield cls


async def test_config_flow_creates_entry(hass: HomeAssistant, mock_api, mock_mqtt):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    # The official app's signing key isn't shipped: App ID / App Key start empty and are required.
    fields = {str(key): key for key in result["data_schema"].schema}
    for name in ("app_id", "app_key"):
        assert fields[name].default is vol.UNDEFINED and isinstance(fields[name], vol.Required)
    user_input = {k: v for k, v in ENTRY_DATA.items() if k != "client_device_id"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(result["data"]["client_device_id"]) == 32


async def test_config_flow_invalid_auth(hass: HomeAssistant, mock_api):
    mock_api.login.side_effect = VividmooAuthError("bad sign")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["errors"] == {"base": "invalid_auth"}


async def test_setup_entities_and_mqtt_push(hass: HomeAssistant, mock_api, mock_mqtt):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, options={"enable_mqtt": True}, unique_id="me@example.com")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.kitchen_box_uses_since_counter_reset").state == "501"
    assert hass.states.get("sensor.kitchen_box_waste_bin_level").state == "100"
    assert hass.states.get("sensor.kitchen_box_last_cat").state == "Moo"
    weight = hass.states.get("sensor.kitchen_box_last_weight")
    assert (weight.state, weight.attributes["unit_of_measurement"]) == ("5.9", "kg")
    duration = hass.states.get("sensor.kitchen_box_last_visit_duration")
    assert (duration.state, duration.attributes["unit_of_measurement"]) == ("36", "s")
    assert hass.states.get("sensor.kitchen_box_last_use").state == "2026-10-11T15:31:58+00:00"
    assert hass.states.get("binary_sensor.kitchen_box_online").state == STATE_ON
    assert hass.states.get("binary_sensor.kitchen_box_waste_bin_full").state == STATE_ON
    assert hass.states.get("sensor.kitchen_box_work_state").state == "unknown"

    # MQTT listener was started with the IM credentials, against the API host.
    host, creds = mock_mqtt.call_args.args[1:3]
    assert host == "apis-eu.airrobo-home.com" and creds.sub_topic == "sub"

    on_frame = mock_mqtt.call_args.args[4]
    all_state = messages.message_class(messages.COMMANDS[(2, 1024)]["ph"])
    env = risp.ImEnvelope(sender="555", recipient="1", content_type="custom", created_at=0, cid="c")

    def push(cmd, msg):
        on_frame(env, risp.RispFrame(dev=2, cmd=cmd, attr=risp.Attr.PUBLISH, seq=1, payload=msg.SerializeToString()))

    # The box's state push is cmd 1024 carrying the full all-state message (app: xb/w.c).
    push(1024, all_state(w_mode=2, w_state=1, lid_state=1))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_box_work_mode").state == "clean"
    assert hass.states.get("sensor.kitchen_box_work_state").state == "running"
    assert hass.states.get("sensor.kitchen_box_work_state").attributes["lid_state"] == 1

    # A 1020 push is ignored, as in the app; a partial one (w_mode dropped as 0) must not read as idle.
    push(1020, messages.message_class(messages.COMMANDS[(2, 1020)]["ph"])(w_state=1))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_box_work_mode").state == "clean"

    # Idle mode means an idle job state whatever w_state says (app maps it to PENDING).
    push(1024, all_state(w_mode=0, w_state=1))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_box_work_mode").state == "idle"
    assert hass.states.get("sensor.kitchen_box_work_state").state == "idle"

    assert await hass.config_entries.async_unload(entry.entry_id)
    mock_mqtt.return_value.stop.assert_called_once()


async def test_auth_failure_starts_reauth(hass: HomeAssistant, mock_api, mock_mqtt):
    mock_api.devices.side_effect = VividmooAuthError("expired")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="me@example.com")
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == config_entries.SOURCE_REAUTH for f in flows)


async def test_mqtt_disabled_by_default(hass: HomeAssistant, mock_api, mock_mqtt):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="me@example.com")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    mock_api.im_login.assert_not_called()
    mock_mqtt.assert_not_called()
    assert hass.states.get("sensor.kitchen_box_uses_since_counter_reset").state == "501"


async def test_kicked_session_backs_off(hass: HomeAssistant, mock_api, mock_mqtt):
    """After the official app takes the IM session back, don't re-login on every refresh."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, options={"enable_mqtt": True}, unique_id="me@example.com")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert mock_api.im_login.call_count == 1

    on_lost = mock_mqtt.call_args.args[5]
    on_lost("kicked: another client logged in to IM with this account")
    await hass.async_block_till_done()
    mock_mqtt.return_value.stop.assert_called_once()

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert mock_api.im_login.call_count == 1  # still backing off; REST polling continued
    assert hass.states.get("binary_sensor.kitchen_box_waste_bin_full").state == STATE_ON


async def test_revoked_token_does_not_start_reauth(hass: HomeAssistant, mock_api, mock_mqtt):
    """A token taken by another login (e.g. the official app) is not a credentials problem."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="me@example.com")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    mock_api.devices.side_effect = VividmooTokenRevoked(300)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_box_waste_bin_level").state == "unavailable"
    assert not any(f["context"]["source"] == config_entries.SOURCE_REAUTH for f in hass.config_entries.flow.async_progress())


# -- buttons / commands ---------------------------------------------------------

import asyncio  # noqa: E402

from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402


class FakeBox:
    """Answers published commands the way the real box did in the logpatch2 capture."""

    def __init__(self, hass, mock_mqtt, work_mode=0, ack=True):
        self.hass, self.mock_mqtt, self.work_mode, self.ack = hass, mock_mqtt, work_mode, ack
        self.sent = []  # (envelope, frame, decoded payload)
        inst = mock_mqtt.return_value
        inst.uid = "1000001"
        inst.connected_event = asyncio.Event()
        inst.connected_event.set()
        inst.publish.side_effect = self._publish

    def _publish(self, payload):
        env, frame = risp.decode_mqtt_payload(payload)
        assert risp.verify_frame(frame, messages.parse(messages.COMMAND, env.body).data, risp.crc8)
        self.sent.append((env, frame, risp.decode_payload_message(frame)))
        if not self.ack:
            return
        reply = b""
        if frame.cmd == 1021:
            reply = messages.message_class(messages.COMMANDS[(2, 1021)]["ra"])(w_mode=self.work_mode, w_state=1).SerializeToString()
        ack = risp.RispFrame(dev=2, cmd=frame.cmd, attr=risp.Attr.REQACK, seq=frame.seq, payload=reply)
        on_frame = self.mock_mqtt.call_args.args[4]
        device = risp.ImEnvelope(sender="555", recipient=env.sender, content_type="custom", created_at=0, cid="c")
        self.hass.loop.call_soon_threadsafe(on_frame, device, ack)


async def _setup(hass, options=None):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, options=options or {}, unique_id="me@example.com")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _press(hass, entity_id):
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


async def test_clean_button_sends_app_identical_command(hass: HomeAssistant, mock_api, mock_mqtt):
    box = FakeBox(hass, mock_mqtt)
    await _setup(hass)  # live updates off: the session is opened just for the command
    await _press(hass, "button.kitchen_box_clean")

    env, frame, op = box.sent[0]
    assert (env.sender, env.recipient, env.content_type) == ("1000001", "555", "custom")
    assert env.raw.protocol_version == "1.0.1" and env.raw.type == 0
    assert (frame.dev, frame.cmd, frame.attr) == (2, 1019, risp.Attr.REQUEST)
    assert (op.op_mode, op.op_state) == (1, 1)
    command = messages.parse(messages.COMMAND, env.body)
    assert command.data == messages.parse(messages.COMMAND, risp.build_operate_command(frame.seq, 1, 1)).data
    mock_mqtt.return_value.stop.assert_called_once()  # on-demand session closed again


async def test_pause_uses_running_mode_from_state_request(hass: HomeAssistant, mock_api, mock_mqtt):
    box = FakeBox(hass, mock_mqtt, work_mode=3)  # smoothing
    await _setup(hass)
    await _press(hass, "button.kitchen_box_pause")
    assert [f.cmd for _, f, _ in box.sent] == [1021, 1019]
    assert (box.sent[1][2].op_mode, box.sent[1][2].op_state) == (3, 2)  # SMOOTH + PAUSE, as captured
    assert hass.states.get("sensor.kitchen_box_work_mode").state == "smooth"


async def test_resume_with_known_mode_skips_state_request(hass: HomeAssistant, mock_api, mock_mqtt):
    box = FakeBox(hass, mock_mqtt)
    await _setup(hass, {"enable_mqtt": True})
    on_frame = mock_mqtt.call_args.args[4]
    paused = messages.message_class(messages.COMMANDS[(2, 1024)]["ph"])(w_mode=2, w_state=3).SerializeToString()
    on_frame(risp.ImEnvelope(sender="555", recipient="1", content_type="custom", created_at=0, cid="c"),
             risp.RispFrame(dev=2, cmd=1024, attr=risp.Attr.PUSH, seq=1, payload=paused))
    await _press(hass, "button.kitchen_box_resume")
    assert [(f.cmd, op.op_mode, op.op_state) for _, f, op in box.sent] == [(1019, 1, 3)]  # CLEAN + RESUME
    mock_mqtt.return_value.stop.assert_not_called()  # live updates on: session stays open


async def test_pause_when_idle_raises(hass: HomeAssistant, mock_api, mock_mqtt):
    box = FakeBox(hass, mock_mqtt, work_mode=0)
    await _setup(hass)
    with pytest.raises(HomeAssistantError, match="idle"):
        await _press(hass, "button.kitchen_box_pause")
    assert [f.cmd for _, f, _ in box.sent] == [1021]


async def test_missing_ack_raises(hass: HomeAssistant, mock_api, mock_mqtt, monkeypatch):
    monkeypatch.setattr("custom_components.vividmoo.coordinator.COMMAND_ACK_TIMEOUT", 0.05)
    FakeBox(hass, mock_mqtt, ack=False)
    await _setup(hass)
    with pytest.raises(HomeAssistantError, match="did not acknowledge"):
        await _press(hass, "button.kitchen_box_level_litter")


async def test_empty_button_disabled_by_default(hass: HomeAssistant, mock_api, mock_mqtt):
    FakeBox(hass, mock_mqtt)
    await _setup(hass)
    entry = er.async_get(hass).async_get("button.kitchen_box_empty_litter_dumps_all_litter")
    assert entry is not None and entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get("button.kitchen_box_clean") is not None


async def test_other_clients_ack_with_same_seq_is_not_ours(hass: HomeAssistant, mock_api, mock_mqtt, monkeypatch):
    """The box fans replies out to every linked account; a foreign 1021 ack must not complete our 1019."""
    monkeypatch.setattr("custom_components.vividmoo.coordinator.COMMAND_ACK_TIMEOUT", 0.2)
    box = FakeBox(hass, mock_mqtt, ack=False)
    await _setup(hass)

    def publish(payload):
        box._publish(payload)
        _, frame, _ = box.sent[-1]
        foreign = risp.RispFrame(dev=2, cmd=1021, attr=risp.Attr.REQACK, seq=frame.seq, payload=b"")
        device = risp.ImEnvelope(sender="555", recipient="other", content_type="custom", created_at=0, cid="c")
        hass.loop.call_soon_threadsafe(mock_mqtt.call_args.args[4], device, foreign)

    mock_mqtt.return_value.publish.side_effect = publish
    with pytest.raises(HomeAssistantError, match="did not acknowledge"):
        await _press(hass, "button.kitchen_box_clean")


async def test_waste_bin_emptied_resets_counter_and_refreshes(hass: HomeAssistant, mock_api, mock_mqtt):
    await _setup(hass)
    calls = mock_api.box_use_times.call_count
    mock_api.box_use_times.return_value = {"boxUseTimes": 0, "boxFullMax": 50, "boxFullAlert": 25, "percentage": 0}
    await _press(hass, "button.kitchen_box_waste_bin_emptied_reset_counter")
    await hass.async_block_till_done()
    mock_api.reset_box_use_times.assert_awaited_once_with("SN1")
    assert mock_api.box_use_times.call_count > calls
    assert hass.states.get("sensor.kitchen_box_waste_bin_level").state == "0"
    mock_mqtt.assert_not_called()  # REST only: no IM session needed


async def test_waste_bin_emptied_failure_raises(hass: HomeAssistant, mock_api, mock_mqtt):
    from custom_components.vividmoo.api import VividmooError
    await _setup(hass)
    mock_api.reset_box_use_times.side_effect = VividmooError("code 403 no permission")
    with pytest.raises(HomeAssistantError, match="Resetting the waste bin counter failed"):
        await _press(hass, "button.kitchen_box_waste_bin_emptied_reset_counter")


# -- card + derived sensors ---------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL  # noqa: E402

import custom_components.vividmoo as vividmoo_init  # noqa: E402


async def test_card_served_by_integration(hass: HomeAssistant):
    import hashlib
    from homeassistant.components.frontend import DATA_EXTRA_JS_URL_ES5

    modern, legacy = MagicMock(), MagicMock()
    hass.data[DATA_EXTRA_MODULE_URL] = modern
    hass.data[DATA_EXTRA_JS_URL_ES5] = legacy
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    assert await vividmoo_init.async_register_card(hass)
    assert await vividmoo_init.async_register_card(hass)  # idempotent across entries/reloads

    (config,) = hass.http.async_register_static_paths.await_args.args[0]
    assert config.url_path == "/vividmoo/vividmoo-card.js"
    assert config.path.endswith("custom_components/vividmoo/frontend/vividmoo-card.js")
    source = open(config.path, "rb").read()
    assert b'customElements.define("vividmoo-card"' in source
    url = f"/vividmoo/vividmoo-card.js?v={hashlib.sha256(source).hexdigest()[:12]}"
    modern.add.assert_called_once_with(url)  # index.html import()s this on every browser
    legacy.add.assert_called_once_with(url)  # and _ls()-loads it as a classic script on the legacy build
    hass.http.async_register_static_paths.assert_awaited_once()


async def test_card_registration_skipped_without_frontend(hass: HomeAssistant):
    hass.data.pop(DATA_EXTRA_MODULE_URL, None)
    assert not await vividmoo_init.async_register_card(hass)


def _visit(rid, iso, event_type=3, weight=5.9, cat_id=6, cat="Mochi"):
    from homeassistant.util import dt as dt_util
    return {"recordId": rid, "eventTime": int(dt_util.parse_datetime(iso).timestamp() * 1000), "eventType": event_type,
            "catInfoId": cat_id if event_type == 3 else 0, "catNickname": cat if event_type == 3 else None,
            "weight": weight, "costTime": 40, "confirmed": 1 if event_type == 3 else 0}


async def test_visit_statistics_and_cat_weight(hass: HomeAssistant, mock_api, mock_mqtt, freezer):
    await hass.config.async_set_time_zone("Europe/Madrid")
    freezer.move_to("2026-10-11T20:00:00+02:00")
    page1 = [  # newest first, as the live API returns them
        _visit(10, "2026-10-11T19:30:00+02:00", event_type=21, weight=2.7),
        _visit(9, "2026-10-11T08:00:00+02:00", weight=6.0),
        _visit(8, "2026-10-11T00:30:00+02:00"),
        _visit(7, "2026-10-10T23:50:00+02:00"),  # yesterday local time
        _visit(6, "2026-10-08T12:00:00+02:00"),
    ]
    page2 = [_visit(5, "2026-10-06T12:00:00+02:00"), _visit(4, "2026-10-04T21:00:00+02:00"),  # 7 d boundary: 10-04 20:00
             _visit(3, "2026-10-04T12:00:00+02:00"), _visit(2, "2026-10-02T12:00:00+02:00")]
    calls = []

    async def records(serial, size=10, message_type=0, before=None):
        calls.append(before)
        if before is None:
            return page1
        return page2 if before == page1[-1]["eventTime"] else []

    mock_api.records.side_effect = records
    await _setup(hass)

    assert calls[:2] == [None, page1[-1]["eventTime"]]  # backfill with the oldest eventTime as cursor
    assert hass.states.get("sensor.kitchen_box_visits_today").state == "3"  # 00:30, 08:00, 19:30 local
    today = hass.states.get("sensor.kitchen_box_visits_today")
    assert (today.attributes["identified_in_history"], today.attributes["unidentified_in_history"]) == (7, 1)
    # Last 7x24 h: ids 10,9,8,7,6,5,4 = 7 visits -> 1.0 per day (ids 3 and 2 are older; 2 is pruned at 8 days).
    assert hass.states.get("sensor.kitchen_box_visits_per_day_7_day_average").state == "1.0"
    weight = hass.states.get("sensor.kitchen_box_cat_weight")
    assert (weight.state, weight.attributes["unit_of_measurement"], weight.attributes["state_class"]) == ("6.0", "kg", "measurement")
    assert weight.attributes["cat"] == "Mochi"  # the newer 2.7 unidentified visit is ignored
    assert hass.states.get("sensor.kitchen_box_last_weight").state == "2.7"  # raw last visit stays available

    # Later polls only fetch the newest page; the history window keeps working.
    calls.clear()
    freezer.move_to("2026-10-12T09:00:00+02:00")
    await vividmoo_entry_refresh(hass)
    assert calls == [None]
    assert hass.states.get("sensor.kitchen_box_visits_today").state == "0"


async def vividmoo_entry_refresh(hass):
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


# -- job timing + state sync on connect -----------------------------------------------

def _push(mock_mqtt, cmd, w_mode, w_state, ts, cause=0):
    msg = messages.message_class(messages.COMMANDS[(2, 1024)]["ph"])(w_mode=w_mode, w_state=w_state, w_cause=cause, timestamp=ts)
    on_frame = mock_mqtt.call_args.args[4]
    attr = risp.Attr.PUSH if cmd == 1024 else risp.Attr.REQACK
    on_frame(risp.ImEnvelope(sender="555", recipient="1", content_type="custom", created_at=0, cid="c"),
             risp.RispFrame(dev=2, cmd=cmd, attr=attr, seq=1, payload=msg.SerializeToString()))


async def test_job_timing_follows_captured_clean_timeline(hass: HomeAssistant, mock_api, mock_mqtt):
    FakeBox(hass, mock_mqtt)
    await _setup(hass, {"enable_mqtt": True})
    t0 = 1791736366  # box timestamps from the logpatch2 clean (start, pause, resume, pause, resume, done)
    attrs = lambda: hass.states.get("sensor.kitchen_box_work_state").attributes  # noqa: E731

    _push(mock_mqtt, 1024, 2, 1, t0, 50)
    await hass.async_block_till_done()
    a = attrs()
    assert a["job_timing_known"] and a["job_run_seconds"] == 0 and a["job_expected_seconds"] == 90
    assert a["job_started"] == a["job_running_since"] == "2026-10-11T16:32:46+00:00"

    _push(mock_mqtt, 1024, 2, 3, t0 + 3, 60)  # pause
    await hass.async_block_till_done()
    assert (attrs()["job_run_seconds"], attrs()["job_running_since"]) == (3.0, None)
    _push(mock_mqtt, 1024, 2, 1, t0 + 5, 50)  # resume
    _push(mock_mqtt, 1024, 2, 3, t0 + 8, 60)  # pause
    _push(mock_mqtt, 1024, 2, 1, t0 + 18, 50)  # resume
    await hass.async_block_till_done()
    assert attrs()["job_run_seconds"] == 6.0 and attrs()["job_running_since"] is not None
    # 6 s + (done 467 - resume 384 = 83 s) = 89 s of real run time, matching the observed ~90 s.

    _push(mock_mqtt, 1024, 0, 1, t0 + 101, 90)  # done: mode omitted, state 1
    await hass.async_block_till_done()
    assert attrs()["job_timing_known"] is False and attrs()["job_started"] is None
    assert hass.states.get("sensor.kitchen_box_work_state").state == "idle"


async def test_snapshot_mid_job_has_unknown_timing(hass: HomeAssistant, mock_api, mock_mqtt):
    FakeBox(hass, mock_mqtt)
    await _setup(hass, {"enable_mqtt": True})
    _push(mock_mqtt, 1021, 3, 1, 1791736480)  # e.g. after a restart while leveling
    await hass.async_block_till_done()
    a = hass.states.get("sensor.kitchen_box_work_state").attributes
    assert hass.states.get("sensor.kitchen_box_work_mode").state == "smooth"
    assert a["job_timing_known"] is False and a["job_expected_seconds"] == 50


async def test_state_requested_on_every_connect(hass: HomeAssistant, mock_api, mock_mqtt):
    box = FakeBox(hass, mock_mqtt, work_mode=2)
    await _setup(hass, {"enable_mqtt": True})
    assert hass.states.get("sensor.kitchen_box_work_mode").state == "unknown"  # nothing pushed yet

    on_connected = mock_mqtt.call_args.args[6]
    on_connected()  # what VividmooMqtt calls after CONNACK (also after automatic reconnects)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [f.cmd for _, f, _ in box.sent] == [1021]
    assert hass.states.get("sensor.kitchen_box_work_mode").state == "clean"

    on_connected()  # reconnect
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [f.cmd for _, f, _ in box.sent] == [1021, 1021]


# -- cat presence (ha_cat_visit.log, 2026-10-12) ----------------------------------------

def _raw(mock_mqtt, cmd, payload_hex, attr=risp.Attr.PUSH):
    on_frame = mock_mqtt.call_args.args[4]
    on_frame(risp.ImEnvelope(sender="555", recipient="1", content_type="custom", created_at=0, cid="c"),
             risp.RispFrame(dev=2, cmd=cmd, attr=attr, seq=1, payload=bytes.fromhex(payload_hex)))


async def test_cat_inside_follows_captured_visit(hass: HomeAssistant, mock_api, mock_mqtt):
    FakeBox(hass, mock_mqtt)
    await _setup(hass, {"enable_mqtt": True})
    cat = "binary_sensor.kitchen_box_cat_inside"
    assert hass.states.get(cat).state == "unavailable"  # nothing pushed or requested yet

    # Cat steps in: idle mode, w_state 2 (blocked), cause 90, toilet_state 1, weigh_state 1.
    _raw(mock_mqtt, 1024, "1002185a200130019001b2cbb0d606")
    await hass.async_block_till_done()
    state = hass.states.get(cat)
    assert state.state == STATE_ON and state.attributes["device_class"] == "occupancy"
    assert state.attributes["presence"] == "in_box" and state.attributes["weighing"] is True
    assert hass.states.get("sensor.kitchen_box_work_state").state == "idle"

    # 42 s later it leaves: toilet_state dropped (0), then a 1023 msg 2000 / msg_type 0 (cat record).
    _raw(mock_mqtt, 1024, "1001185a9001dccbb0d606")
    _raw(mock_mqtt, 1023, "08cac52e10d00f")
    await hass.async_block_till_done()
    state = hass.states.get(cat)
    assert state.state == STATE_OFF
    assert (state.attributes["presence"], state.attributes["weighing"]) == ("away", False)

    # The auto clean that followed (mode 2, cause 50) doesn't touch presence.
    _raw(mock_mqtt, 1024, "0802100118329001b4d0b0d606")
    await hass.async_block_till_done()
    assert hass.states.get(cat).state == STATE_OFF
    assert hass.states.get("sensor.kitchen_box_work_mode").state == "clean"


async def test_cat_inside_unavailable_when_session_lost(hass: HomeAssistant, mock_api, mock_mqtt):
    FakeBox(hass, mock_mqtt)
    await _setup(hass, {"enable_mqtt": True})
    _raw(mock_mqtt, 1024, "1002185a200130019001b2cbb0d606")
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_box_cat_inside").state == STATE_ON

    mock_mqtt.call_args.args[5]("kicked: another client logged in to IM with this account")
    await hass.async_block_till_done()
    # A stale "on" would be wrong: we'd miss the exit push while disconnected.
    assert hass.states.get("binary_sensor.kitchen_box_cat_inside").state == "unavailable"
