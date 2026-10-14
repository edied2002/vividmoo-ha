"""X-UBT-* request signing.

Mirrors com.ubtrobot.infrastructure.a0 (UrcsSignInterceptor) in the Android app:

    X-UBT-Sign = md5_hex(ts + app_key + nonce + device_id) + " " + ts + " " + nonce + " v2"

where ts is epoch milliseconds (server-corrected) and nonce is the first 10 chars
of a random UUID4 string. The algorithm is plain Java in the APK; only the
APP_ID / APP_KEY *values* are hidden inside the VMP-protected code.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time
import uuid


def md5_hex(value: str) -> str:
    """Lower-case hex MD5 of a UTF-8 string (androidx.compose.animation.core.h0.f)."""
    return hashlib.md5(value.encode("utf-8")).hexdigest()  # noqa: S324 - protocol-defined


def make_sign(app_key: str, device_id: str, ts_ms: int, nonce: str) -> str:
    """Build an X-UBT-Sign value from explicit inputs."""
    return f"{md5_hex(f'{ts_ms}{app_key}{nonce}{device_id}')} {ts_ms} {nonce} v2"


def new_nonce() -> str:
    """Return a nonce in the app's format: uuid4 string truncated to 10 chars."""
    return str(uuid.uuid4())[:10]


@dataclass
class Signer:
    """Produces the X-UBT-* header set for one app/client identity."""

    app_id: str
    app_key: str
    device_id: str
    clock_offset_ms: int = 0

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.clock_offset_ms

    def headers(self, ts_ms: int | None = None, nonce: str | None = None) -> dict[str, str]:
        ts_ms = self.now_ms() if ts_ms is None else ts_ms
        nonce = new_nonce() if nonce is None else nonce
        return {
            "X-UBT-AppId": self.app_id,
            "X-UBT-DeviceId": self.device_id,
            "X-UBT-Sign": make_sign(self.app_key, self.device_id, ts_ms, nonce),
        }
