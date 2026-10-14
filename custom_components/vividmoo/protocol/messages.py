"""Runtime protobuf message classes built from the APK's embedded descriptors.

Building classes at runtime from serialized FileDescriptorProtos avoids shipping
protoc-generated *_pb2.py modules, which are tied to a specific protobuf
runtime version (Home Assistant pins its own).
"""

from __future__ import annotations

from functools import cache

from google.protobuf import (
    any_pb2,
    descriptor_pb2,
    descriptor_pool,
    message_factory,
    wrappers_pb2,
)
from google.protobuf.message import Message

from .descriptors import DESCRIPTORS

BYTES_VALUE_TYPE_URL = "type.googleapis.com/google.protobuf.BytesValue"


@cache
def _pool() -> descriptor_pool.DescriptorPool:
    pool = descriptor_pool.DescriptorPool()
    for dep in (descriptor_pb2, any_pb2):
        proto = descriptor_pb2.FileDescriptorProto()
        dep.DESCRIPTOR.CopyToProto(proto)
        pool.Add(proto)
    for raw in DESCRIPTORS.values():
        pool.Add(descriptor_pb2.FileDescriptorProto.FromString(raw))
    return pool


def message_class(full_name: str) -> type[Message]:
    """Return the message class for a fully-qualified name, e.g. 'rosa.dev.rpc.X'."""
    return _message_class(full_name)


@cache
def _message_class(full_name: str) -> type[Message]:
    descriptor = _pool().FindMessageTypeByName(full_name)
    if hasattr(message_factory, "GetMessageClass"):  # protobuf >= 4.21
        return message_factory.GetMessageClass(descriptor)
    return message_factory.MessageFactory(_pool()).GetPrototype(descriptor)  # pragma: no cover


def parse(full_name: str, data: bytes) -> Message:
    msg = message_class(full_name)()
    msg.ParseFromString(data)
    return msg


# Fully-qualified names used by the integration.
IM_MESSAGE = "rosa.im.protobuf.Message"
COMMAND = "Command"  # rosa/command/rpc/Command.proto has no package
RISP_HEADER = "rosa.transport.risp.Header"
SERVER_PUSH_INFO = "ServerPushInfo"
SERVER_APP_INFO = "server_app_info"

DEV_PKG = "rosa.dev.rpc."
UPDATE_PKG = "rosa.update.rpc."
VISION_PKG = "rosa.vision.rpc."

# (dev, cmd) -> message names by direction. dev 2 = litter box main controller,
# dev 0x20 = vision module, dev 0x1a = LED belt. Names follow d<dev hex>c<cmd>.
# "ra" = response to our request, "ph" = device push; "rq" = request we would send.
COMMANDS: dict[tuple[int, int], dict[str, str]] = {
    (2, 1009): {"rq": DEV_PKG + "d2c1009_intelligent_dev_fault_get_rq", "ra": DEV_PKG + "d2c1009_intelligent_dev_fault_get_ra"},
    (2, 1019): {"rq": DEV_PKG + "d2c1019_intelligent_dev_airpet_op_rq", "ra": DEV_PKG + "d2c1019_intelligent_dev_airpet_op_ra"},
    # The app ignores 1020 pushes (xb/w.c has no case for them). Decoded only for logging.
    (2, 1020): {"ph": DEV_PKG + "d2c1020_intelligent_dev_airpet_work_state_change_ph"},
    (2, 1021): {"rq": DEV_PKG + "d2c1021_intelligent_dev_airpet_all_state_get_rq", "ra": DEV_PKG + "d2c1021_intelligent_dev_airpet_all_state_get_ra"},
    # The box's live state push: cmd 1024 carrying the full d2c1021 all-state payload (xb/w.c -> f()).
    (2, 1024): {"ph": DEV_PKG + "d2c1021_intelligent_dev_airpet_all_state_get_ra"},
    (2, 2536): {"ph": DEV_PKG + "d2c_intelligent_dev_delete_ph"},
    (2, 2537): {"ph": DEV_PKG + "d2c_intelligent_dev_delete_ph"},
    (2, 5001): {"ph": SERVER_APP_INFO},
    (2, 1023): {"ph": DEV_PKG + "d2c1023_intelligent_dev_airpet_msg_update_ph"},
    (2, 2500): {"rq": DEV_PKG + "d2c2500_intelligent_dev_fault_get_rq", "ra": DEV_PKG + "d2c2500_intelligent_dev_fault_get_ra"},
    (2, 2503): {"ph": DEV_PKG + "d2c2503_intelligent_dev_self_checking_report_ph"},
    (2, 2506): {"ph": DEV_PKG + "d2c2506_intelligent_dev_update_report_ph"},
    (2, 2527): {"ph": DEV_PKG + "d2c2527_intelligent_dev_debug_info_report_ph"},
    (2, 2534): {"ph": DEV_PKG + "d2c2534_intelligent_dev_info_record_ph"},
    (2, 2535): {"rq": DEV_PKG + "d2c2535_intelligent_dev_config_info_get_rq", "ra": DEV_PKG + "d2c2535_intelligent_dev_config_info_get_ra"},
    (2, 4): {"rq": UPDATE_PKG + "s4_sw_ver_rq", "ra": UPDATE_PKG + "s4_sw_ver_ra"},
    (2, 155): {"rq": UPDATE_PKG + "s155_dev_update_progress_check_rq", "ra": UPDATE_PKG + "s155_dev_update_progress_check_ra"},
    (2, 156): {"ph": UPDATE_PKG + "s156_dev_update_progress_report_ph"},
    (2, 158): {"ph": UPDATE_PKG + "s158_dev_update_progress_report_ph"},
    (2, 159): {"ph": UPDATE_PKG + "s159_update_result_report_ph"},
}


# The Any field inside our private pool is a different class from any_pb2.Any,
# so pack and unpack through its type_url/value fields rather than Pack/Unpack.
def pack_bytes_value(target: Message, data: bytes) -> None:
    target.type_url = BYTES_VALUE_TYPE_URL
    target.value = wrappers_pb2.BytesValue(value=data).SerializeToString()


def unpack_bytes_value(packed: Message) -> bytes | None:
    if packed.type_url != BYTES_VALUE_TYPE_URL:
        return None
    return wrappers_pb2.BytesValue.FromString(packed.value).value
