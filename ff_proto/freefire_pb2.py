# -*- coding: utf-8 -*-
# source: freefire.proto
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import symbol_database as _symbol_database
from google.protobuf.internal import builder as _builder

_sym_db = _symbol_database.Default()

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(b'\n\x0efreefire.proto"t\n\x08LoginReq\x12\x0f\n\x07open_id\x18\x01 \x01(\t\x12\x14\n\x0copen_id_type\x18\x02 \x01(\t\x12\x13\n\x0blogin_token\x18\x03 \x01(\t\x12\x1b\n\x13orign_platform_type\x18\x04 \x01(\t\x12\x0f\n\x07app_key\x18\x05 \x01(\t".\n\x08LoginRes\x12\r\n\x05token\x18\x01 \x01(\t\x12\x13\n\x0block_region\x18\x02 \x01(\tb\x06proto3')

_globals = globals()
_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, _globals)
_builder.BuildTopDescriptorsAndMessages(DESCRIPTOR, 'freefire_pb2', _globals)
if _descriptor._USE_C_DESCRIPTORS == False:
    DESCRIPTOR._options = None
    _globals['_LOGINREQ']._serialized_start = 18
    _globals['_LOGINREQ']._serialized_end = 134
    _globals['_LOGINRES']._serialized_start = 136
    _globals['_LOGINRES']._serialized_end = 182
