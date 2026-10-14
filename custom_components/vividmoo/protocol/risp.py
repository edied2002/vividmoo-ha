"""RISP framing and the IM envelope that carries it over MQTT.

Wire layers (outermost first), from cf.a / xb.f / wf.j in the APK:

    rosa.im.protobuf.Message{type=PRIVATE, protocol_version="1.0.1",
      message_content{content_type="custom", content=Any(BytesValue(
        Command{protype=1, data=<RISP frame>}))}}

    RISP frame = 0xDB | hdr_len:u8 | Header | crc8(Header) | payload | crc8(payload)
                 (payload and its CRC byte are absent when Header.dataLen == 0)

CRC-8 is CRC-8/I-432-1 (see crc8). Decoding doesn't verify CRCs; encoding takes the CRC function as a parameter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
import logging
import random
import time

from . import messages

_LOGGER = logging.getLogger(__name__)

FRAME_MAGIC = 0xDB
IM_PROTOCOL_VERSION = "1.0.1"
CONTENT_TYPE_CUSTOM = "custom"
COMMAND_PROTYPE_RISP = 1

Crc8 = Callable[[bytes], int]


def crc8(data: bytes) -> int:
    """CRC-8/I-432-1 (poly 0x07, init 0x00, no reflection, xorout 0x55).

    Recovered from 18 CrcUtil input/output pairs captured with logpatch2, and verified
    against both CRC bytes of all 37 captured frames (app and device, lengths 2-14).
    """
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0x55


# d2c1019 airpet_op_rq values, captured from the app (DeviceOperateRequest ordinals).
OP_MODE_CLEAN = 1
OP_MODE_EMPTY = 2  # INFERRED: the remaining OpMode ordinal (CLEAR); not captured yet
OP_MODE_SMOOTH = 3
OP_STATE_START = 1
OP_STATE_PAUSE = 2
OP_STATE_RESUME = 3
# The state push reports airpet_work_mode (CLEAN=2, SMOOTH=3, CLEAR=4), a different numbering.
# Pause/resume resend the running job's op_mode, as the app does.
WORK_MODE_TO_OP_MODE = {2: OP_MODE_CLEAN, 3: OP_MODE_SMOOTH, 4: OP_MODE_EMPTY}
DEV_MAIN = 2
CMD_OPERATE = 1019
CMD_ALL_STATE = 1021


class RispError(ValueError):
    """Raised for malformed frames or envelopes."""


class Attr(IntEnum):
    RESERVE = 0
    REQUEST = 1
    REQACK = 2
    PUBLISH = 3
    PUBACK = 4
    NOTIFY = 5
    PUSH = 6


@dataclass
class RispFrame:
    dev: int
    cmd: int
    attr: int
    seq: int
    payload: bytes = b""
    ack: int = 0
    id: int = 0
    header_crc: int | None = None
    payload_crc: int | None = None

    @property
    def key(self) -> tuple[int, int]:
        return (self.dev, self.cmd)


def decode_frame(data: bytes) -> RispFrame:
    if len(data) < 3 or data[0] != FRAME_MAGIC:
        raise RispError("invalid RISP frame header")
    hdr_len = data[1]
    hdr_end = 2 + hdr_len
    if len(data) < hdr_end + 1:
        raise RispError("truncated RISP header")
    header = messages.parse(messages.RISP_HEADER, data[2:hdr_end])
    header_crc = data[hdr_end]
    payload, payload_crc = b"", None
    if header.dataLen > 0:
        start = hdr_end + 1
        end = start + header.dataLen
        if len(data) < end + 1:
            raise RispError("truncated RISP payload")
        payload, payload_crc = data[start:end], data[end]
    return RispFrame(
        dev=header.dev,
        cmd=header.cmd,
        attr=int(header.attr),
        seq=header.seq,
        payload=payload,
        ack=header.ack,
        id=header.id,
        header_crc=header_crc,
        payload_crc=payload_crc,
    )


def encode_frame(frame: RispFrame, crc8: Crc8) -> bytes:
    header = messages.message_class(messages.RISP_HEADER)(
        dev=frame.dev,
        cmd=frame.cmd,
        id=frame.id,
        dataLen=len(frame.payload),
        attr=frame.attr,
        ack=frame.ack,
        seq=frame.seq,
    ).SerializeToString()
    out = bytes([FRAME_MAGIC, len(header)]) + header + bytes([crc8(header) & 0xFF])
    if frame.payload:
        out += frame.payload + bytes([crc8(frame.payload) & 0xFF])
    return out


def build_request_command(seq: int, cmd: int, payload: bytes = b"", dev: int = DEV_MAIN) -> bytes:
    """Command bytes wrapping one RISP REQUEST frame (protype 1), as the app's RispMessageConverter builds them."""
    frame = encode_frame(RispFrame(dev=dev, cmd=cmd, attr=Attr.REQUEST, seq=seq, payload=payload), crc8)
    return messages.message_class(messages.COMMAND)(protype=COMMAND_PROTYPE_RISP, data=frame).SerializeToString()


def build_operate_command(seq: int, op_mode: int, op_state: int) -> bytes:
    """d2c1019 (dev 2, cmd 1019, REQUEST), byte-identical to the app's frames."""
    payload = messages.message_class(messages.COMMANDS[(DEV_MAIN, CMD_OPERATE)]["rq"])(
        op_mode=op_mode, op_state=op_state
    ).SerializeToString()
    return build_request_command(seq, CMD_OPERATE, payload)


def build_state_request_command(seq: int) -> bytes:
    """d2c1021 all-state request (empty payload), as the app sends on opening the box page."""
    return build_request_command(seq, CMD_ALL_STATE)


def new_cid() -> str:
    """Per-message id in the app's format (xf/i.java via c0.b.q()): '<epoch ms>+<6 digits>'."""
    return f"{int(time.time() * 1000)}+{random.randint(100000, 999999)}"


def verify_frame(frame: RispFrame, raw: bytes, crc8: Crc8) -> bool:
    """Check both CRC bytes of a decoded frame against a candidate CRC-8."""
    hdr_len = raw[1]
    if crc8(raw[2 : 2 + hdr_len]) & 0xFF != frame.header_crc:
        return False
    return not frame.payload or crc8(frame.payload) & 0xFF == frame.payload_crc


@dataclass
class ImEnvelope:
    sender: str
    recipient: str
    content_type: str
    created_at: int
    cid: str
    body: bytes | None = None  # Command bytes for "custom" content
    raw: object = field(default=None, repr=False)


def decode_envelope(data: bytes) -> ImEnvelope:
    msg = messages.parse(messages.IM_MESSAGE, data)
    content = msg.message_content
    body = None
    if content.content_type == CONTENT_TYPE_CUSTOM:
        body = messages.unpack_bytes_value(content.content)
    return ImEnvelope(
        sender=getattr(msg, "from"),
        recipient=msg.to,
        content_type=content.content_type,
        created_at=msg.created_at,
        cid=msg.cid,
        body=body,
        raw=msg,
    )


def encode_envelope(sender: str, recipient: str, command: bytes, cid: str | None = None) -> bytes:
    msg = messages.message_class(messages.IM_MESSAGE)()
    msg.cid = cid or new_cid()
    msg.created_at = int(time.time() * 1000)
    setattr(msg, "from", sender)
    msg.to = recipient
    msg.type = 0  # PRIVATE
    msg.protocol_version = IM_PROTOCOL_VERSION
    msg.message_content.content_type = CONTENT_TYPE_CUSTOM
    messages.pack_bytes_value(msg.message_content.content, command)
    return msg.SerializeToString()


def decode_mqtt_payload(data: bytes) -> tuple[ImEnvelope, RispFrame | None]:
    """Decode an MQTT payload down to the RISP frame, when there is one."""
    env = decode_envelope(data)
    if env.body is None:
        return env, None
    command = messages.parse(messages.COMMAND, env.body)
    if command.protype != COMMAND_PROTYPE_RISP:
        _LOGGER.debug("Ignoring Command with protype %s", command.protype)
        return env, None
    return env, decode_frame(command.data)


def decode_payload_message(frame: RispFrame) -> object | None:
    """Parse a frame's payload with the matching device message, if known."""
    names = messages.COMMANDS.get(frame.key)
    if not names:
        return None
    if frame.attr == Attr.REQACK:
        name = names.get("ra")
    elif frame.attr == Attr.REQUEST:
        name = names.get("rq")
    else:
        name = names.get("ph") or names.get("ra")
    return messages.parse(name, frame.payload) if name else None
