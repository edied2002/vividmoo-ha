"""Live check against the Vividmoo cloud using the integration's own REST client.

Phase 1 (no credentials): unsigned clock sync, then a signed-but-unauthenticated
request with a valid and a deliberately broken X-UBT-Sign to prove the formula.
Phase 2 (credentials): real login + device list.

Credentials come from VIVIDMOO_ACCOUNT / VIVIDMOO_PASSWORD / VIVIDMOO_APP_ID / VIVIDMOO_APP_KEY or from
~/.config/vividmoo/credentials with lines `account=...`, `password=...`, `app_id=...` and `app_key=...`.
The password is never printed; tokens and serial numbers are masked.
"""

import asyncio
import importlib
import json
import os
import pathlib
import re
import ssl
import sys
import types

import aiohttp

ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "vividmoo"
pkg = types.ModuleType("vividmoo_live")
pkg.__path__ = [str(ROOT)]
sys.modules["vividmoo_live"] = pkg
api_mod = importlib.import_module("vividmoo_live.api")
const = importlib.import_module("vividmoo_live.const")
sign = importlib.import_module("vividmoo_live.protocol.sign")

CLIENT_DEVICE_ID = os.environ.get("VIVIDMOO_CLIENT_DEVICE_ID", "0" * 32)


def mask(value, keep=4):
    """Show at most `keep` chars at each end, and never more than a third of the value."""
    value = str(value)
    keep = min(keep, len(value) // 6)
    return f"{value[:keep]}…{value[-keep:]}" if keep else f"…({len(value)})"


def _credentials_file():
    path = pathlib.Path.home() / ".config" / "vividmoo" / "credentials"
    if not path.exists():
        return {}
    return {k.strip(): v.strip() for k, v in (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)}


def load_credentials():
    account, password = os.environ.get("VIVIDMOO_ACCOUNT"), os.environ.get("VIVIDMOO_PASSWORD")
    if not (account and password):
        values = _credentials_file()
        account, password = values.get("account", ""), values.get("password", "")
    return (account, password) if account and password else (None, None)


def load_app_identity():
    values = _credentials_file()
    app_id = os.environ.get("VIVIDMOO_APP_ID") or values.get("app_id")
    app_key = os.environ.get("VIVIDMOO_APP_KEY") or values.get("app_key")
    if not (app_id and app_key):
        sys.exit("Set VIVIDMOO_APP_ID / VIVIDMOO_APP_KEY or app_id= / app_key= in ~/.config/vividmoo/credentials (see README)")
    return app_id, app_key


async def raw_signed_get(session, api, path, headers):
    async with session.get(f"{api.base_url}{path}", headers={"X-UBT-Language": "en", **headers}) as resp:
        text = await resp.text()
        return resp.status, text[:300]


async def main():
    account, password = load_credentials()
    app_id, app_key = load_app_identity()
    async with aiohttp.ClientSession() as session:
        api = api_mod.VividmooApi(
            session, const.DEFAULT_BASE_URL, app_id, app_key, CLIENT_DEVICE_ID,
            account or "", password or "", const.DEFAULT_ACCOUNT_TYPE, const.DEFAULT_PASSWORD_MODE,
        )
        print(f"base_url={api.base_url}")
        await api.sync_clock()
        print(f"[1] clock sync: offset {api._signer.clock_offset_ms} ms")

        good = api._signer.headers()
        bad = dict(good, **{"X-UBT-Sign": "0" * 32 + good["X-UBT-Sign"][32:]})
        for label, headers in (("valid sign", good), ("broken sign", bad)):
            status, body = await raw_signed_get(session, api, "/catbox-server/app/area", headers)
            print(f"[1] GET /catbox-server/app/area ({label}): HTTP {status} {body[:90]}")

        if not account:
            print("[2] skipped: no credentials (set VIVIDMOO_ACCOUNT/VIVIDMOO_PASSWORD or ~/.config/vividmoo/credentials)")
            return
        try:
            token = await api.login()
        except api_mod.VividmooError as err:
            print(f"[2] login FAILED: {err}")
            return
        print(f"[2] login OK: token {mask(token.token)} expireAt={token.expire_at} userId={token.user_id}")
        try:
            devices = await api.devices()
        except api_mod.VividmooError as err:
            print(f"[2] device list FAILED: {err}")
            return
        print(f"[2] device list OK: {len(devices)} device(s)")
        for d in devices:
            print(f"    serial={mask(d.get('serialNumber'))} name={d.get('deviceName')!r} status={d.get('deviceStatus')!r} "
                  f"shareFlag={d.get('shareFlag')!r} keys={sorted(d)}")

        box = next((d for d in devices if not BOX_NAME or d.get("deviceName") == BOX_NAME), None)
        if box is None:
            print(f"[3] skipped: no device named {BOX_NAME!r}")
            return
        serial = box["serialNumber"]
        secrets_to_mask.add(serial)
        await phase3_rest(session, api, serial)
        await phase4_mqtt(api, serial)


# ---------------------------------------------------------------- phase 3 --

BOX_NAME = os.environ.get("VIVIDMOO_BOX_NAME", "")  # empty: the first box
MQTT_SECONDS = int(os.environ.get("VIVIDMOO_MQTT_SECONDS", "120"))
secrets_to_mask: set[str] = set()
SECRET_KEYS = {"token", "refreshToken", "password", "mqttPassword", "mqttUserName", "clientId", "accessToken",
               "zgoAppSign", "zgoAppId"}
OPAQUE = re.compile(r"^[A-Za-z0-9+/=_\-.]{24,}$")


def redact(value, key=None):
    """Mask serials, tokens and other opaque strings; keep everything else."""
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if isinstance(value, str):
        if key in SECRET_KEYS:
            return f"<{key}:{len(value)} chars>"
        if OPAQUE.match(value):
            return mask(value)
        for s in secrets_to_mask:
            value = value.replace(s, mask(s))
    return value


async def raw(session, api, method, path, *, params=None, body=None):
    headers = {"X-UBT-Language": "en", "authorization": api.token.token, **api._signer.headers()}
    async with session.request(method, f"{api.base_url}{path}", params=params, json=body, headers=headers) as resp:
        text = await resp.text()
        try:
            data = json.loads(text)
        except ValueError:
            data = text[:300]
        return resp.status, data


def show(label, status, data, limit=1500):
    text = json.dumps(redact(data), ensure_ascii=False)
    print(f"[3] {label}: HTTP {status} {text[:limit]}{' …' if len(text) > limit else ''}")


async def phase3_rest(session, api, serial):
    calls = [
        ("box config", "GET", "/catbox-server/box/config/allConfig", {"serialNumber": serial}, None, lambda: api.box_config(serial)),
        ("use counts", "GET", "/catbox-server/box/config/box-use-times", {"serialNumber": serial}, None, lambda: api.box_use_times(serial)),
        ("records", "POST", "/catbox-server/web/box/record/new", None, {"messageType": 0, "size": 3, "serialNumber": serial}, lambda: api.records(serial, size=3)),
        ("IM contacts", "GET", "/v1/ubtechinc-im-manager/im/friends", None, None, api.im_contacts),
    ]
    for label, method, path, params, body, call in calls:
        status, data = await raw(session, api, method, path, params=params, body=body)
        show(f"{label} raw", status, data)
        try:
            parsed = await call()
            kind = f"list[{len(parsed)}]" if isinstance(parsed, list) else type(parsed).__name__
            if label == "records" and parsed:
                r = parsed[0]
                kind += f" newest: eventTime={r.get('eventTime')} cat={r.get('catNickname')!r} weight={r.get('weight')} costTime={r.get('costTime')} eventType={r.get('eventType')}"
            print(f"[3] {label} via integration: OK -> {kind}")
        except api_mod.VividmooError as err:
            print(f"[3] {label} via integration: FAILED {err}")


# ---------------------------------------------------------------- phase 4 --

async def phase4_mqtt(api, serial):
    if MQTT_SECONDS <= 0:
        print("[4] skipped (VIVIDMOO_MQTT_SECONDS=0)")
        return
    import paho.mqtt.client as paho
    risp = importlib.import_module("vividmoo_live.protocol.risp")

    try:
        creds = await api.im_login()
    except api_mod.VividmooError as err:
        print(f"[4] IM login FAILED: {err}")
        return
    for value in (creds.client_id, creds.username, creds.password):
        secrets_to_mask.add(value)
    print(f"[4] IM login OK: clientId={mask(creds.client_id)} user={mask(creds.username)} uid={creds.uid} "
          f"sub={redact(creds.sub_topic)} pub={redact(creds.pub_topic)}")

    host = api.base_url.split("://", 1)[1].split("/", 1)[0]
    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue()
    client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=creds.client_id, clean_session=True)
    client.username_pw_set(creds.username, creds.password)
    client.tls_set_context(ssl.create_default_context())
    client.on_connect = lambda c, u, f, rc, p=None: loop.call_soon_threadsafe(events.put_nowait, ("connect", rc))
    client.on_subscribe = lambda c, u, mid, rcs, p=None: loop.call_soon_threadsafe(events.put_nowait, ("subscribed", rcs))
    client.on_disconnect = lambda c, u, f, rc, p=None: loop.call_soon_threadsafe(events.put_nowait, ("disconnect", rc))
    client.on_message = lambda c, u, m: loop.call_soon_threadsafe(events.put_nowait, ("message", (m.topic, bytes(m.payload))))

    print(f"[4] connecting to {host}:21000 (TLS) for {MQTT_SECONDS}s, subscribe only")
    try:
        client.connect_async(host, 21000, keepalive=20)
        client.loop_start()
    except Exception as err:  # noqa: BLE001
        print(f"[4] connect FAILED: {err!r}")
        return
    deadline = loop.time() + MQTT_SECONDS
    messages = 0
    try:
        while (remaining := deadline - loop.time()) > 0:
            try:
                kind, data = await asyncio.wait_for(events.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if kind == "connect":
                print(f"[4] CONNACK: {data}")
                if not getattr(data, "is_failure", False):
                    client.subscribe(creds.sub_topic, qos=1)
            elif kind == "subscribed":
                print(f"[4] SUBACK: {[str(r) for r in data]}")
            elif kind == "disconnect":
                print(f"[4] disconnected: {data}")
            else:
                messages += 1
                topic, payload = data
                describe_payload(risp, messages, redact(topic), payload)
    finally:
        client.disconnect()
        client.loop_stop()
    print(f"[4] done: {messages} message(s) in {MQTT_SECONDS}s")


def describe_payload(risp, n, topic, payload):
    print(f"[4] #{n} topic={topic} {len(payload)} bytes")
    try:
        env, frame = risp.decode_mqtt_payload(payload)
    except Exception as err:  # noqa: BLE001
        print(f"      undecodable as IM envelope ({err!r}); head={payload[:48].hex()}")
        return
    print(f"      IM from={redact(env.sender)} to={redact(env.recipient)} content_type={env.content_type!r} "
          f"protocol={env.raw.protocol_version!r} type_url={env.raw.message_content.content.type_url!r}")
    if frame is None:
        print(f"      non-RISP body; content bytes head={env.raw.message_content.content.value[:48].hex()}")
        return
    print(f"      RISP dev={frame.dev} cmd={frame.cmd} attr={frame.attr} seq={frame.seq} payload={frame.payload.hex()}")
    try:
        msg = risp.decode_payload_message(frame)
    except Exception as err:  # noqa: BLE001
        print(f"      payload parse failed: {err!r}")
        return
    if msg is not None:
        print(f"      {type(msg).__name__}: {{{', '.join(f'{f.name}={v}' for f, v in msg.ListFields())}}}")


if __name__ == "__main__":
    asyncio.run(main())
