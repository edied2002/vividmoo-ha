"""REST client tests against a local fake server (no Home Assistant needed)."""

import hashlib
import importlib
import pathlib
import sys
import types

import aiohttp
from aiohttp import web
import pytest

# Load the integration's modules as a stub package so __init__.py (which
# imports homeassistant) is not executed.
_PKG = "vividmoo_stub"
if _PKG not in sys.modules:
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(pathlib.Path(__file__).parents[1] / "custom_components" / "vividmoo")]
    sys.modules[_PKG] = pkg
api_mod = importlib.import_module(f"{_PKG}.api")

# The Home Assistant test harness blocks sockets; these tests use a local server.
pytestmark = pytest.mark.enable_socket

HA_USER_AGENT = "HomeAssistant/2026.2.3 aiohttp/3.13.3 Python/3.13"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += minutes * 60


def check_sign(request: web.Request, app_key="KEY") -> None:
    digest, ts, nonce, version = request.headers["X-UBT-Sign"].split(" ")
    assert version == "v2" and len(nonce) == 10
    device = request.headers["X-UBT-DeviceId"]
    assert digest == hashlib.md5(f"{ts}{app_key}{nonce}{device}".encode()).hexdigest()
    assert request.headers["X-UBT-AppId"] == "APPID"


@pytest.fixture
async def server(aiohttp_server):
    state = {"logins": 0, "login_bodies": [], "device_list_401": 0, "device_list_calls": 0,
             "bad_password": False, "use_times_mode": "ok", "user_agents": set(), "reset_bodies": []}

    @web.middleware
    async def api_filter(request, handler):
        # Mirrors the live API's filter: Home Assistant's default User-Agent gets 403.
        ua = request.headers.get("User-Agent", "")
        state["user_agents"].add(ua)
        if "HomeAssistant" in ua:
            return web.json_response({"message": "Access denied"}, status=403)
        return await handler(request)

    async def login(request):
        check_sign(request)
        assert "authorization" not in request.headers
        state["login_bodies"].append(await request.json())
        if state["bad_password"]:
            return web.json_response({"code": 2002, "message": "用户或密码错误"}, status=400)
        state["logins"] += 1
        return web.json_response({"token": {"token": f"tok{state['logins']}", "expireAt": 1}, "user": {"userId": 7}})

    async def device_list(request):
        check_sign(request)
        state["device_list_calls"] += 1
        if state["device_list_401"]:
            state["device_list_401"] -= 1
            return web.Response(status=401)
        assert request.headers["authorization"] == f"tok{state['logins']}"
        return web.json_response({"code": 0, "message": "ok", "data": [{"serialNumber": "SN1", "deviceName": "Box"}]})

    async def use_times(request):
        assert request.query["serialNumber"] == "SN1"
        if state["use_times_mode"] == "illegal_client":
            return web.json_response({"code": 1003, "message": "非法客户端"}, status=403)
        return web.json_response({"code": 200, "data": {"boxUseTimes": 3, "percentage": 30}})

    async def records(request):
        body = await request.json()
        # The live server returns nothing without messageType; the app never sends page.
        assert body == {"messageType": 0, "size": 1, "serialNumber": "SN1"}
        return web.json_response({"code": 0, "message": "success", "success": True, "data": {
            "records": [{"recordId": 2, "eventTime": 1_791_732_718_000, "weight": 5.9}], "total": 1, "size": 10, "current": 1, "pages": 1}})

    async def reset_use_times(request):
        check_sign(request)
        assert request.headers["authorization"] == f"tok{state['logins']}"
        state["reset_bodies"].append(await request.json())
        return web.json_response({"code": 0, "message": "success", "data": None, "success": True})

    async def failing(request):
        return web.json_response({"code": 120, "message": "nope"})

    async def im_login(request):
        check_sign(request)
        assert await request.json() == {"type": 2}
        return web.json_response({"code": 0, "msg": "", "currentTimes": 1, "data": {
            "clientId": "c", "mqttUserName": "u", "mqttPassword": "p", "pubTopic": "pub", "subTopic": "sub", "uid": 5}})

    async def timestamp(request):
        assert "X-UBT-Sign" not in request.headers
        return web.json_response({"timestamp": 1_000})  # live shape: epoch seconds, no envelope

    app = web.Application(middlewares=[api_filter])
    app.router.add_put("/user-service-rest/v2/user/login", login)
    app.router.add_get("/user-service-rest/v2/robot/common/device/list", device_list)
    app.router.add_get("/catbox-server/box/config/box-use-times", use_times)
    app.router.add_get("/catbox-server/box/config/allConfig", failing)
    app.router.add_put("/catbox-server/box/config/box-use-times/reset", reset_use_times)
    app.router.add_post("/catbox-server/web/box/record/new", records)
    app.router.add_post("/v1/ubtechinc-im-manager/im/login", im_login)
    app.router.add_get("/v1/client-auth-service/api/timestamp", timestamp)
    srv = await aiohttp_server(app)
    return srv, state


def make_api(session, srv, password_mode="plain", clock=None):
    api = api_mod.VividmooApi(
        session, str(srv.make_url("")), "APPID", "KEY", "DEV0123456789abc", "me@example.com", "secret", 1, password_mode
    )
    if clock is not None:
        api._monotonic = clock
    return api


async def test_user_agent_is_app_okhttp_even_on_ha_session(server):
    """HA's shared session defaults to a HomeAssistant/... UA, which the live API rejects."""
    srv, state = server
    async with aiohttp.ClientSession(headers={"User-Agent": HA_USER_AGENT}) as session:
        api = make_api(session, srv)
        await api.sync_clock()
        assert await api.devices() == [{"serialNumber": "SN1", "deviceName": "Box"}]
    assert state["user_agents"] == {"okhttp/4.10.0"}


async def test_api_filter_403_is_not_an_auth_error(server):
    srv, _ = server
    async with aiohttp.ClientSession() as session:
        resp = await session.get(srv.make_url("/catbox-server/box/config/box-use-times"), headers={"User-Agent": HA_USER_AGENT})
        assert resp.status == 403 and await resp.json() == {"message": "Access denied"}


async def test_normal_expiry_relogins_once(server):
    srv, state = server
    clock = FakeClock()
    async with aiohttp.ClientSession() as session:
        api = make_api(session, srv, clock=clock)
        await api.login()
        clock.advance(30)  # token expired long after our login: not a revocation
        state["device_list_401"] = 1
        assert await api.devices() == [{"serialNumber": "SN1", "deviceName": "Box"}]
    assert state["logins"] == 2
    assert state["login_bodies"][0] == {"account": "me@example.com", "accountType": 1, "password": "secret"}


async def test_revoked_token_backs_off_instead_of_relogging(server):
    srv, state = server
    clock = FakeClock()
    async with aiohttp.ClientSession() as session:
        api = make_api(session, srv, clock=clock)
        await api.login()
        clock.advance(2)
        state["device_list_401"] = 1  # another login took our token shortly after we logged in
        with pytest.raises(api_mod.VividmooTokenRevoked) as err:
            await api.devices()
        assert err.value.retry_in == 5 * 60
        assert state["logins"] == 1  # no instant re-login

        calls = state["device_list_calls"]
        with pytest.raises(api_mod.VividmooTokenRevoked):
            await api.devices()  # still inside the backoff: no network call at all
        assert state["device_list_calls"] == calls and state["logins"] == 1

        clock.advance(5.1)
        assert await api.devices()  # backoff over: one fresh login, works again
        assert state["logins"] == 2

        clock.advance(1)
        state["device_list_401"] = 1  # revoked again soon after: backoff doubles
        with pytest.raises(api_mod.VividmooTokenRevoked) as err:
            await api.devices()
        assert err.value.retry_in == 10 * 60

        clock.advance(10.1)
        assert await api.devices()
        clock.advance(15)  # stable for longer than the revoke window: counter resets
        assert await api.devices()
        state["device_list_401"] = 1
        clock.advance(15)  # a 401 long after login is a normal expiry again
        assert await api.devices()
    assert state["logins"] == 4


async def test_wrong_password_is_auth_error(server):
    srv, state = server
    state["bad_password"] = True
    async with aiohttp.ClientSession() as session:
        with pytest.raises(api_mod.VividmooAuthError, match="wrong account or password"):
            await make_api(session, srv).login()


async def test_illegal_client_is_auth_error(server):
    srv, state = server
    state["use_times_mode"] = "illegal_client"
    async with aiohttp.ClientSession() as session:
        with pytest.raises(api_mod.VividmooAuthError, match="signature rejected"):
            await make_api(session, srv).box_use_times("SN1")


async def test_md5_password_mode(server):
    srv, state = server
    async with aiohttp.ClientSession() as session:
        await make_api(session, srv, "md5").login()
    assert state["login_bodies"][0]["password"] == hashlib.md5(b"secret").hexdigest()


async def test_error_code_raises(server):
    srv, _ = server
    async with aiohttp.ClientSession() as session:
        with pytest.raises(api_mod.VividmooError):
            await make_api(session, srv).box_config("SN1")


async def test_im_login_maps_mqtt_fields(server):
    srv, _ = server
    async with aiohttp.ClientSession() as session:
        creds = await make_api(session, srv).im_login()
    assert (creds.username, creds.password, creds.sub_topic, creds.uid) == ("u", "p", "sub", "5")


async def test_clock_sync_is_unsigned(server):
    srv, _ = server
    async with aiohttp.ClientSession() as session:
        api = make_api(session, srv)
        await api.sync_clock()
    assert api._signer.clock_offset_ms < 0


async def test_records_uses_app_request_shape(server):
    srv, _ = server
    async with aiohttp.ClientSession() as session:
        records = await make_api(session, srv).records("SN1", size=1)
    assert records[0]["recordId"] == 2


async def test_reset_box_use_times_matches_app_body(server):
    srv, state = server
    async with aiohttp.ClientSession() as session:
        await make_api(session, srv).reset_box_use_times("SN1")
    assert state["reset_bodies"] == [{"boxUseTimes": 0, "serialNumber": "SN1"}]  # as UbtBoxFullModifier sends it
