"""Async REST client for the Vividmoo (UBTech airpet) cloud.

Paths, bodies and DTO field names come from the Retrofit interfaces in the
APK (see vividmoo-analysis/endpoints.txt). Every request is signed with the
X-UBT-* headers; logged-in requests also carry the raw token in
`authorization` (no "Bearer" prefix appears anywhere in the app).
"""

from __future__ import annotations

from dataclasses import dataclass
import json as _json
import logging
import time
from typing import Any

import aiohttp

from .const import (
    APP_USER_AGENT,
    IM_LOGIN_TYPE,
    PASSWORD_MODE_MD5,
    REVOKE_BACKOFF_INITIAL,
    REVOKE_BACKOFF_MAX,
    REVOKE_WINDOW,
    SUCCESS_CODES,
)
from .protocol.sign import Signer, md5_hex

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=20)

RECORDS_CAT = 0
RECORDS_DEVICE = 1


class VividmooError(Exception):
    """Generic API failure."""


class VividmooAuthError(VividmooError):
    """Credentials or app identity were rejected; the user has to act (reauth)."""


class VividmooTokenRevoked(VividmooError):
    """Our token keeps getting revoked, most likely by another login to the same account.

    Observed live: when the official app re-authenticates, the integration's token gets
    HTTP 401. This is not a credentials problem, so it must not start a reauth flow.
    """

    def __init__(self, retry_in: float) -> None:
        super().__init__(f"token revoked by another login; retrying in {retry_in / 60:.0f} min")
        self.retry_in = retry_in


# Server error codes seen live.
CODE_BAD_CREDENTIALS = 2002  # HTTP 400 {"code":2002,"message":"用户或密码错误"}
CODE_ILLEGAL_CLIENT = 1003  # HTTP 403 {"code":1003,"message":"非法客户端"} (bad X-UBT-Sign / app key)


@dataclass
class Token:
    token: str
    refresh_token: str | None
    expire_at: int | None
    user_id: int | None


@dataclass
class MqttCredentials:
    client_id: str
    username: str
    password: str
    pub_topic: str
    sub_topic: str
    uid: str


class VividmooApi:
    """Signed REST client. One instance per config entry."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        app_id: str,
        app_key: str,
        client_device_id: str,
        account: str,
        password: str,
        account_type: int,
        password_mode: str,
    ) -> None:
        self._session = session
        self.base_url = base_url.rstrip("/")
        self._signer = Signer(app_id, app_key, client_device_id)
        self._account = account
        self._password = password
        self._account_type = account_type
        self._password_mode = password_mode
        self.token: Token | None = None
        self._monotonic = time.monotonic
        self._last_login_at: float | None = None
        self._revocations = 0
        self._blocked_until = 0.0

    # -- low level ---------------------------------------------------------

    async def sync_clock(self) -> None:
        """Correct the signing clock (the app syncs before signing too)."""
        try:
            data = await self._request("GET", "/v1/client-auth-service/api/timestamp", signed=False, auth=False)
        except VividmooError as err:
            _LOGGER.debug("Clock sync failed, using local time: %s", err)
            return
        # Live response is {"timestamp": <epoch seconds>} with no envelope. The app
        # signs with this second-resolution time too (its sign timestamps trail
        # the wall clock by <1 s), so the truncation is intentional.
        server = data.get("timestamp") if isinstance(data, dict) else data
        if isinstance(server, int) and server > 0:
            server_ms = server * 1000 if server < 10**12 else server
            self._signer.clock_offset_ms = server_ms - int(time.time() * 1000)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        signed: bool = True,
        auth: bool = True,
        retry_auth: bool = True,
    ) -> Any:
        # The API sits behind a filter that rejects Home Assistant's default
        # User-Agent with 403 {"message":"Access denied"} (reproduced live); send the app's.
        headers = {"User-Agent": APP_USER_AGENT, "X-UBT-Language": "en"}
        if signed:
            headers.update(self._signer.headers())
        if auth:
            self._check_backoff()
            if self.token is None:
                await self.login()
            headers["authorization"] = self.token.token
        url = f"{self.base_url}{path}"
        try:
            async with self._session.request(method, url, params=params, json=json, headers=headers, timeout=TIMEOUT) as resp:
                text = await resp.text()
                status = resp.status
        except aiohttp.ClientError as err:
            raise VividmooError(f"{method} {path}: {err}") from err
        try:
            body = _json.loads(text) if text else None
        except ValueError:
            body = None
        code = body.get("code") if isinstance(body, dict) else None

        if status == 401 and auth:
            self.token = None
            if retry_auth and self._may_relogin():
                return await self._request(method, path, params=params, json=json, signed=signed, auth=auth, retry_auth=False)
            raise self._revoked()
        if code == CODE_BAD_CREDENTIALS:
            raise VividmooAuthError(f"{method} {path}: wrong account or password (code {code})")
        if code == CODE_ILLEGAL_CLIENT:
            raise VividmooAuthError(f"{method} {path}: request signature rejected (code {code}); check App ID/App Key")
        if status == 403:
            raise VividmooError(f"{method} {path}: HTTP 403 from the API filter: {text[:200]}")
        if status >= 400:
            raise VividmooError(f"{method} {path}: HTTP {status} {text[:200]}")
        if body is None and text:
            raise VividmooError(f"{method} {path}: non-JSON response {text[:200]}")
        if auth:
            self._note_authorized_success()
        return _unwrap(method, path, body)

    # -- token revocation backoff --------------------------------------------

    def _check_backoff(self) -> None:
        remaining = self._blocked_until - self._monotonic()
        if remaining > 0:
            raise VividmooTokenRevoked(remaining)

    def _may_relogin(self) -> bool:
        """A normal expiry re-logs in at once; a 401 soon after our own login means revocation."""
        return self._last_login_at is None or self._monotonic() - self._last_login_at >= REVOKE_WINDOW.total_seconds()

    def _revoked(self) -> VividmooTokenRevoked:
        self._revocations += 1
        backoff = min(
            REVOKE_BACKOFF_INITIAL.total_seconds() * 2 ** (self._revocations - 1),
            REVOKE_BACKOFF_MAX.total_seconds(),
        )
        self._blocked_until = self._monotonic() + backoff
        _LOGGER.warning(
            "Vividmoo token revoked %s time(s) in a row, probably by another login to the same account "
            "(e.g. the official app). Pausing for %.0f min. Use a separate account with the box shared to it",
            self._revocations,
            backoff / 60,
        )
        return VividmooTokenRevoked(backoff)

    def _note_authorized_success(self) -> None:
        if self._revocations and self._last_login_at is not None and (
            self._monotonic() - self._last_login_at >= REVOKE_WINDOW.total_seconds()
        ):
            self._revocations = 0

    # -- account -----------------------------------------------------------

    async def login(self) -> Token:
        password = md5_hex(self._password) if self._password_mode == PASSWORD_MODE_MD5 else self._password
        data = await self._request(
            "PUT",
            "/user-service-rest/v2/user/login",
            json={"account": self._account, "accountType": self._account_type, "password": password},
            auth=False,
        )
        token = (data or {}).get("token") or {}
        if not token.get("token"):
            raise VividmooAuthError("Login response contained no token")
        user = (data or {}).get("user") or {}
        self.token = Token(token["token"], token.get("refreshToken"), token.get("expireAt"), user.get("userId"))
        self._last_login_at = self._monotonic()
        return self.token

    # -- devices -----------------------------------------------------------

    async def devices(self) -> list[dict[str, Any]]:
        """CatBox{serialNumber, deviceName, deviceStatus, adminUserNickName, shareFlag}."""
        return await self._request("GET", "/user-service-rest/v2/robot/common/device/list") or []

    async def box_config(self, serial: str) -> dict[str, Any]:
        """CatBoxConfig: switches, schedules, wifiName, boxFunctionVOList ..."""
        return await self._request("GET", "/catbox-server/box/config/allConfig", params={"serialNumber": serial}) or {}

    async def box_use_times(self, serial: str) -> dict[str, Any]:
        """BoxUseTimes{boxUseTimes, boxFullMax, boxFullAlert, percentage, alertType}."""
        return await self._request("GET", "/catbox-server/box/config/box-use-times", params={"serialNumber": serial}) or {}

    async def reset_box_use_times(self, serial: str) -> None:
        """Reset the waste-bin use counter (BoxUseTimes.boxUseTimes / percentage).

        Same call and body as the app's UbtBoxFullModifier (md/b.java): PUT with boxUseTimes=0.
        """
        await self._request(
            "PUT",
            "/catbox-server/box/config/box-use-times/reset",
            json={"boxUseTimes": 0, "serialNumber": serial},
        )

    async def records(
        self, serial: str, size: int = 10, message_type: int = RECORDS_CAT, before: int | None = None
    ) -> list[dict[str, Any]]:
        """Newest-first records (RecordBean.records).

        Same body as the app's first page (ue/f.java): messageType is
        MessageType.ordinal() (CAT=0 visits, DEVICE=1 device events). eventTime is
        the paging cursor and is omitted for the first page. The live server
        ignores size (it returned 10-30 records for size=3) and returns nothing
        at all without messageType.

        Paging (verified live): pass `before` = the oldest eventTime of the previous
        page; the next page is strictly older, with no overlap.
        """
        body: dict[str, Any] = {"messageType": message_type, "size": size, "serialNumber": serial}
        if before is not None:
            body["eventTime"] = before
        data = await self._request("POST", "/catbox-server/web/box/record/new", json=body)
        return (data or {}).get("records") or []

    # -- IM / MQTT ---------------------------------------------------------

    async def im_login(self) -> MqttCredentials:
        data = await self._request("POST", "/v1/ubtechinc-im-manager/im/login", json={"type": IM_LOGIN_TYPE}) or {}
        try:
            return MqttCredentials(
                client_id=data["clientId"],
                username=data["mqttUserName"],
                password=data["mqttPassword"],
                pub_topic=data["pubTopic"],
                sub_topic=data["subTopic"],
                uid=str(data.get("uid", "")),
            )
        except KeyError as err:
            raise VividmooError(f"IM login response missing {err}") from err

    async def im_contacts(self) -> list[dict[str, Any]]:
        """ContactDTO list; a device's contact has userName == serial number."""
        return await self._request("GET", "/v1/ubtechinc-im-manager/im/friends") or []


def _unwrap(method: str, path: str, body: Any) -> Any:
    """Unwrap ServerResponse{code,message,data} / UrcsResponse{code,msg,data}."""
    if not isinstance(body, dict) or "code" not in body:
        return body  # e.g. login returns NetworkAccountInfo directly
    code = body.get("code")
    if code not in SUCCESS_CODES:
        message = body.get("message") or body.get("msg")
        raise VividmooError(f"{method} {path}: code {code} {message}")
    return body.get("data")
