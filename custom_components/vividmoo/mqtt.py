"""Subscribe-only listener for the cloud IM/MQTT channel.

Connects with the credentials from /im/login (MQTT over TLS, port 21000,
keepalive 20s, clean session), subscribes to subTopic and decodes pushes down
to RISP frames. It never publishes: sending commands needs the CRC-8
parameters and op_mode/op_state values, which are not recovered yet.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import ssl

import paho.mqtt.client as paho

from homeassistant.core import HomeAssistant, callback

from .api import MqttCredentials
from .const import MQTT_KEEPALIVE, MQTT_PORT
from .protocol import risp

_LOGGER = logging.getLogger(__name__)

FrameCallback = Callable[[risp.ImEnvelope, risp.RispFrame], None]
LostCallback = Callable[[str], None]

# IM content_type of rosa.im.protobuf.ForceOfflineNotification. Observed live: when the
# official app logs in to IM again, our session gets {type:"offline/kicked", cause:"kick off by others"}
# and every reconnect with the same credentials is refused ("Not authorized").
CONTENT_TYPE_OFFLINE = "offline"


class VividmooMqtt:
    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        creds: MqttCredentials,
        ssl_context: ssl.SSLContext,
        on_frame: FrameCallback,
        on_lost: LostCallback,
        on_connected: Callable[[], None] | None = None,
    ) -> None:
        self._hass = hass
        self._host = host
        self._creds = creds
        self._on_frame = on_frame
        self._on_lost = on_lost
        self._on_connected = on_connected
        self._lost = False
        self.connected_event = asyncio.Event()
        try:
            self._client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=creds.client_id, clean_session=True)
        except AttributeError:  # paho-mqtt < 2.0
            self._client = paho.Client(client_id=creds.client_id, clean_session=True)
        self._client.username_pw_set(creds.username, creds.password)
        self._client.tls_set_context(ssl_context)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=2, max_delay=120)
        self.connected = False

    def start(self) -> None:
        """Start the network loop (blocking connect happens in paho's thread)."""
        self._client.connect_async(self._host, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        self._client.loop_start()

    @property
    def uid(self) -> str:
        """Our IM user id (envelope 'from')."""
        return self._creds.uid

    def publish(self, payload: bytes) -> None:
        """Publish one IM envelope to our pubTopic (the app publishes there too)."""
        info = self._client.publish(self._creds.pub_topic, payload, qos=1)
        if info.rc != paho.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"MQTT publish failed: rc={info.rc}")

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    # paho callbacks run in paho's thread.

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", reason_code != 0):
            # Credentials from /im/login are single-use per session; retrying them is pointless.
            self._session_lost(f"connect refused: {reason_code}")
            return
        self.connected = True
        _LOGGER.info("MQTT connected; subscribing to %s", self._creds.sub_topic)
        client.subscribe(self._creds.sub_topic, qos=1)
        self._hass.loop.call_soon_threadsafe(self.connected_event.set)
        if self._on_connected is not None:  # also after paho's automatic reconnects
            self._hass.loop.call_soon_threadsafe(self._on_connected)

    def _on_disconnect(self, client, userdata, *args) -> None:
        self.connected = False
        self._hass.loop.call_soon_threadsafe(self.connected_event.clear)
        _LOGGER.info("MQTT disconnected: %s", args)

    def _on_message(self, client, userdata, message) -> None:
        try:
            env, frame = risp.decode_mqtt_payload(message.payload)
        except Exception:  # noqa: BLE001 - never let a bad frame kill the loop
            _LOGGER.debug("Undecodable MQTT payload on %s: %s", message.topic, message.payload.hex())
            return
        if frame is None:
            if env.content_type == CONTENT_TYPE_OFFLINE:
                self._session_lost("kicked: another client logged in to IM with this account")
                return
            _LOGGER.debug("Non-RISP IM message from %s (content_type=%s)", env.sender, env.content_type)
            return
        _LOGGER.debug(
            "RISP from %s: dev=%s cmd=%s attr=%s seq=%s payload=%s",
            env.sender, frame.dev, frame.cmd, frame.attr, frame.seq, frame.payload.hex(),
        )
        self._hass.loop.call_soon_threadsafe(self._dispatch, env, frame)

    def _session_lost(self, reason: str) -> None:
        """Stop paho's auto-reconnect (disconnect() ends its loop) and tell the coordinator once."""
        self.connected = False
        if self._lost:
            return
        self._lost = True
        self._client.disconnect()
        self._hass.loop.call_soon_threadsafe(self._on_lost, reason)

    @callback
    def _dispatch(self, env: risp.ImEnvelope, frame: risp.RispFrame) -> None:
        self._on_frame(env, frame)
