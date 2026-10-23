"""Decode local Firestore Document/Value protobufs without loading an SDK."""

import struct

from arc_to_zen.leveldb_readonly import blob, varint


def fields(data):
    result, pos = {}, 0
    while pos < len(data):
        tag, pos = varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("Invalid protobuf field number")
        if wire == 0:
            value, pos = varint(data, pos)
        elif wire == 2:
            value, pos = blob(data, pos)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if pos + size > len(data):
                raise ValueError("Truncated fixed-width protobuf field")
            value, pos = data[pos : pos + size], pos + size
        else:
            raise ValueError("Unsupported protobuf wire type")
        result.setdefault(number, []).append((wire, value))
    return result


def mapping(data, depth):
    if depth > 32:
        raise ValueError("Firestore value nesting exceeds the limit")
    result = {}
    for wire, raw in fields(data).get(1, []):
        if wire != 2:
            raise ValueError("Invalid Firestore map entry")
        entry = fields(raw)
        key = entry[1][-1][1].decode("utf-8")
        result[key] = value(entry[2][-1][1], depth + 1)
    return result


def value(data, depth=0):
    if depth > 32:
        raise ValueError("Firestore value nesting exceeds the limit")
    item = fields(data)
    for number, entries in item.items():
        wire, raw = entries[-1]
        if number in (17, 5) and wire == 2:
            return raw.decode("utf-8")
        if number == 18 and wire == 2:
            return {"bytes_hex": raw.hex()}
        if number == 1 and wire == 0:
            return bool(raw)
        if number == 2 and wire == 0:
            return raw - (1 << 64) if raw >= (1 << 63) else raw
        if number == 3 and wire == 1:
            return struct.unpack("<d", raw)[0]
        if number == 11:
            return None
        if number == 6 and wire == 2:
            return mapping(raw, depth + 1)
        if number == 9 and wire == 2:
            return [value(v, depth + 1) for w, v in fields(raw).get(1, []) if w == 2]
        if number == 10 and wire == 2:
            timestamp = fields(raw)
            return {"seconds": timestamp.get(1, [(0, 0)])[-1][1], "nanos": timestamp.get(2, [(0, 0)])[-1][1]}
    raise ValueError("Unsupported Firestore value type")


def encode_varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def document(data, depth=0):
    """Accept Document directly or wrapped in the local MaybeDocument message."""
    if depth > 3:
        return None
    try:
        parsed = fields(data)
    except ValueError:
        return None
    names = [raw for wire, raw in parsed.get(1, []) if wire == 2 and raw.startswith(b"projects/")]
    if names and b"/documents/" in names[-1]:
        # Document.fields and MapValue.fields both use repeated map entries.
        document_fields = b"".join(
            b"\x0a" + encode_varint(len(raw)) + raw for wire, raw in parsed.get(2, []) if wire == 2
        )
        return names[-1].decode("utf-8"), mapping(document_fields, 0)
    for entries in parsed.values():
        for wire, raw in entries:
            if wire == 2 and b"/documents/" in raw:
                result = document(raw, depth + 1)
                if result:
                    return result
    return None
