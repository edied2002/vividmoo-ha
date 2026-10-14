"""Tests for signing, RISP framing and the IM envelope."""

import hashlib

import pytest

from protocol import messages, risp, sign


def fake_crc8(data: bytes) -> int:
    # Stand-in until the real CRC-8 parameters are recovered; any function
    # exercises the framing because decoding never validates CRCs.
    return sum(data) & 0xFF


def test_sign_matches_app_formula():
    ts, nonce = 1791720782560, "a1b2c3d4-e"
    expected = hashlib.md5(f"{ts}KEY{nonce}DEV".encode()).hexdigest()
    assert sign.make_sign("KEY", "DEV", ts, nonce) == f"{expected} {ts} {nonce} v2"


def test_sign_input_layout_matches_app():
    # Same layout as the MD5 inputs the app logs: ts(13) + app_key(32 hex) + nonce(10) + device id(32 hex).
    ts, nonce, device = 1791732906000, "8655f93f-1", "0123456789abcdef0123456789abcdef"
    app_key = "00112233445566778899aabbccddeeff"
    logged = f"{ts}{app_key}{nonce}{device}"
    assert len(logged) == 13 + 32 + 10 + 32
    assert sign.make_sign(app_key, device, ts, nonce) == f"{hashlib.md5(logged.encode()).hexdigest()} {ts} {nonce} v2"


def test_signer_headers_and_nonce_shape():
    headers = sign.Signer("APPID", "KEY", "DEV").headers(ts_ms=1, nonce="0123456789")
    assert headers["X-UBT-AppId"] == "APPID"
    assert headers["X-UBT-DeviceId"] == "DEV"
    assert headers["X-UBT-Sign"].endswith(" 1 0123456789 v2")
    assert len(sign.new_nonce()) == 10


def test_all_descriptors_load():
    for names in messages.COMMANDS.values():
        for name in names.values():
            messages.message_class(name)
    for name in (messages.IM_MESSAGE, messages.COMMAND, messages.RISP_HEADER, messages.SERVER_PUSH_INFO):
        messages.message_class(name)


def test_frame_roundtrip_with_payload():
    payload = messages.message_class(messages.COMMANDS[(2, 1020)]["ph"])(w_mode=2, w_state=1, w_cause=7).SerializeToString()
    frame = risp.RispFrame(dev=2, cmd=1020, attr=risp.Attr.PUBLISH, seq=42, payload=payload)
    raw = risp.encode_frame(frame, fake_crc8)
    assert raw[0] == 0xDB
    decoded = risp.decode_frame(raw)
    assert (decoded.dev, decoded.cmd, decoded.attr, decoded.seq) == (2, 1020, risp.Attr.PUBLISH, 42)
    assert decoded.payload == payload
    assert risp.verify_frame(decoded, raw, fake_crc8)
    msg = risp.decode_payload_message(decoded)
    assert (msg.w_mode, msg.w_state, msg.w_cause) == (2, 1, 7)


def test_frame_without_payload_has_no_payload_crc():
    raw = risp.encode_frame(risp.RispFrame(dev=2, cmd=1021, attr=risp.Attr.REQUEST, seq=1), fake_crc8)
    hdr_len = raw[1]
    assert len(raw) == 2 + hdr_len + 1
    assert risp.decode_frame(raw).payload == b""


def test_full_mqtt_payload_decode():
    payload = messages.message_class(messages.COMMANDS[(2, 1021)]["ra"])(w_mode=0, lid_state=1, weigh_sensor=4321).SerializeToString()
    frame_bytes = risp.encode_frame(risp.RispFrame(dev=2, cmd=1021, attr=risp.Attr.REQACK, seq=9, payload=payload), fake_crc8)
    command = messages.message_class(messages.COMMAND)(protype=1, data=frame_bytes).SerializeToString()
    mqtt_payload = risp.encode_envelope("device-im-id", "user-im-id", command)

    env, frame = risp.decode_mqtt_payload(mqtt_payload)
    assert env.sender == "device-im-id"
    assert env.content_type == "custom"
    assert env.raw.protocol_version == "1.0.1"
    assert env.raw.message_content.content.type_url == messages.BYTES_VALUE_TYPE_URL
    state = risp.decode_payload_message(frame)
    assert (state.lid_state, state.weigh_sensor) == (1, 4321)


def test_bad_frames_raise():
    with pytest.raises(risp.RispError):
        risp.decode_frame(b"\x00\x01\x02")
    with pytest.raises(risp.RispError):
        risp.decode_frame(bytes([0xDB, 50, 1, 2]))


# Captured with logpatch2 on 2026-10-11 (signed Java bytes as logged).
def _b(signed):
    return bytes(x & 0xFF for x in signed)


CAPTURED_SENDS = {
    # (seq, op_mode, op_state): frame the app sent
    (9568, 1, 1): [-37, 12, 8, 2, 16, -5, 7, 32, 4, 40, 1, 56, -32, 74, -26, 8, 1, 16, 1, -34],  # clean start
    (9569, 1, 2): [-37, 12, 8, 2, 16, -5, 7, 32, 4, 40, 1, 56, -31, 74, -13, 8, 1, 16, 2, -41],  # clean pause
    (9570, 1, 3): [-37, 12, 8, 2, 16, -5, 7, 32, 4, 40, 1, 56, -30, 74, -52, 8, 1, 16, 3, -48],  # clean resume
    (9573, 3, 1): [-37, 12, 8, 2, 16, -5, 7, 32, 4, 40, 1, 56, -27, 74, -89, 8, 3, 16, 1, 8],  # smooth start
    (9574, 3, 2): [-37, 12, 8, 2, 16, -5, 7, 32, 4, 40, 1, 56, -26, 74, -104, 8, 3, 16, 2, 1],  # smooth pause
}


def test_crc8_matches_captured_pairs():
    assert risp.crc8(b"") == 85
    assert risp.crc8(_b([8, 1, 16, 1])) == 222
    assert risp.crc8(_b([8, 3, 16, 2])) == 1
    assert risp.crc8(_b([8, 2, 16, -5, 7, 32, 4, 40, 1, 56, -32, 74])) == 230
    assert risp.crc8(_b([8, 2, 16, -3, 7, 40, 1, 56, -35, 74])) == 130


def test_operate_command_is_byte_identical_to_app():
    for (seq, mode, state), frame in CAPTURED_SENDS.items():
        command = messages.parse(messages.COMMAND, risp.build_operate_command(seq, mode, state))
        assert command.protype == 1
        assert command.data == _b(frame), (seq, mode, state)


def test_captured_device_frames_verify_and_decode():
    ack = _b([-37, 10, 8, 2, 16, -5, 7, 40, 2, 56, -32, 74, 100])  # REQACK for seq 9568
    frame = risp.decode_frame(ack)
    assert (frame.cmd, frame.attr, frame.seq) == (1019, risp.Attr.REQACK, 9568)
    assert risp.verify_frame(frame, ack, risp.crc8)

    push = _b([-37, 14, 8, 2, 16, -128, 8, 24, 1, 32, 13, 40, 6, 56, -4, 8, 53, 8, 2, 16, 1, 24, 50,
               -112, 1, -82, -12, -82, -42, 6, -49])  # state push during clean
    frame = risp.decode_frame(push)
    assert (frame.cmd, frame.attr) == (1024, risp.Attr.PUSH) and risp.verify_frame(frame, push, risp.crc8)
    state = risp.decode_payload_message(frame)
    assert (state.w_mode, state.w_state, state.w_cause) == (2, 1, 50)

    done = _b([-37, 14, 8, 2, 16, -128, 8, 24, 1, 32, 11, 40, 6, 56, -127, 9, -113, 16, 1, 24, 90,
               -112, 1, -109, -11, -82, -42, 6, 87])  # after the clean finished: mode omitted (0), state 1
    state = risp.decode_payload_message(risp.decode_frame(done))
    assert (state.w_mode, state.w_state, state.w_cause) == (0, 1, 90)


def test_cid_format_matches_app():
    import re
    assert re.fullmatch(r"\d{13}\+[1-9]\d{5}", risp.new_cid())
