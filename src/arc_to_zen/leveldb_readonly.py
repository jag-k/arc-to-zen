"""Small read-only LevelDB snapshot reader for Arc's local Firestore cache.

Does not open the database through LevelDB, take its lock, or modify its files.
Follows CURRENT/MANIFEST and applies sequence numbers and deletion tombstones.
Format references: google/leveldb doc/{table_format,log_format}.md.
"""

import struct
from pathlib import Path


LIMIT = 128 * 1024 * 1024


def varint(data, pos=0):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError("Truncated varint")
        part = data[pos]
        pos += 1
        value |= (part & 127) << shift
        if not part & 128:
            return value, pos
    raise ValueError("Oversized varint")


def blob(data, pos):
    size, pos = varint(data, pos)
    if size > LIMIT or pos + size > len(data):
        raise ValueError("Invalid length-prefixed field")
    return data[pos : pos + size], pos + size


def snappy(data):
    size, pos = varint(data)
    if size > LIMIT:
        raise ValueError("Snappy block too large")
    out = bytearray()
    while pos < len(data):
        tag, pos = data[pos], pos + 1
        kind, n = tag & 3, tag >> 2
        if kind == 0:
            if n >= 60:
                count = n - 59
                if pos + count > len(data):
                    raise ValueError("Truncated Snappy length")
                n = int.from_bytes(data[pos : pos + count], "little")
                pos += count
            n += 1
            if pos + n > len(data):
                raise ValueError("Truncated Snappy literal")
            out.extend(data[pos : pos + n])
            pos += n
        else:
            count = 1 if kind == 1 else 2 if kind == 2 else 4
            if pos + count > len(data):
                raise ValueError("Truncated Snappy copy")
            offset = int.from_bytes(data[pos : pos + count], "little")
            pos += count
            if kind == 1:
                offset |= (tag & 224) << 3
                n = 4 + ((tag >> 2) & 7)
            else:
                n += 1
            if offset == 0 or offset > len(out):
                raise ValueError("Invalid Snappy offset")
            for _ in range(n):
                out.append(out[-offset])
        if len(out) > size:
            raise ValueError("Snappy output exceeds declared size")
    if len(out) != size:
        raise ValueError("Snappy size mismatch")
    return bytes(out)


def crc_entry(value):
    for _ in range(8):
        value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
    return value


CRC_TABLE = tuple(crc_entry(i) for i in range(256))


def crc32c(data):
    value = 0xFFFFFFFF
    for byte in data:
        value = CRC_TABLE[(value ^ byte) & 255] ^ (value >> 8)
    return value ^ 0xFFFFFFFF


def check_crc(raw, checksum):
    value = crc32c(raw)
    masked = (((value >> 15) | (value << 17)) + 0xA282EAD8) & 0xFFFFFFFF
    if masked != checksum:
        raise ValueError("LevelDB checksum mismatch; close Arc and retry")


def log_records(data):
    pos, fragments = 0, None
    while pos < len(data):
        remaining = 32768 - (pos % 32768)
        if remaining < 7:
            pos += remaining
            continue
        if pos + 7 > len(data):
            raise ValueError("Incomplete LevelDB log; close Arc and retry")
        checksum, size, kind = struct.unpack_from("<IHB", data, pos)
        pos += 7
        if checksum == size == kind == 0:
            pos += remaining - 7
            continue
        if size > remaining - 7 or pos + size > len(data):
            raise ValueError("Truncated LevelDB log record; close Arc and retry")
        payload = data[pos : pos + size]
        pos += size
        check_crc(bytes([kind]) + payload, checksum)
        if kind == 1:
            if fragments is not None:
                raise ValueError("Unfinished fragmented LevelDB record")
            yield payload
        elif kind == 2:
            if fragments is not None:
                raise ValueError("Invalid LevelDB first fragment")
            fragments = bytearray(payload)
        elif kind in (3, 4) and fragments is not None:
            fragments.extend(payload)
            if len(fragments) > LIMIT:
                raise ValueError("LevelDB record too large")
            if kind == 4:
                yield bytes(fragments)
                fragments = None
        else:
            raise ValueError("Unsupported LevelDB log record type")
    if fragments is not None:
        raise ValueError("Incomplete LevelDB log; close Arc and retry")


def block_entries(data):
    if len(data) < 4:
        raise ValueError("Invalid LevelDB block")
    restarts = struct.unpack_from("<I", data, len(data) - 4)[0]
    end, pos, previous = len(data) - 4 - 4 * restarts, 0, b""
    if end < 0:
        raise ValueError("Invalid LevelDB restart array")
    while pos < end:
        shared, pos = varint(data, pos)
        unshared, pos = varint(data, pos)
        size, pos = varint(data, pos)
        if shared > len(previous) or pos + unshared + size > end:
            raise ValueError("Invalid LevelDB block entry")
        key = previous[:shared] + data[pos : pos + unshared]
        pos += unshared
        value = data[pos : pos + size]
        pos += size
        previous = key
        yield key, value
    if pos != end:
        raise ValueError("LevelDB block boundary mismatch")


def table_entries(data):
    if len(data) < 48 or data[-8:] != bytes.fromhex("57fb808b247547db"):
        raise ValueError("Unsupported LevelDB table footer")
    footer = data[-48:-8]
    _, pos = varint(footer)
    _, pos = varint(footer, pos)
    index_offset, pos = varint(footer, pos)
    index_size, _ = varint(footer, pos)

    def block(offset, size):
        if size > LIMIT or offset + size + 5 > len(data) - 48:
            raise ValueError("Invalid LevelDB block handle")
        raw = data[offset : offset + size]
        kind = data[offset + size]
        checksum = struct.unpack_from("<I", data, offset + size + 1)[0]
        check_crc(raw + bytes([kind]), checksum)
        if kind == 0:
            return raw
        if kind == 1:
            return snappy(raw)
        raise ValueError("Unsupported LevelDB block compression")

    for _, handle in block_entries(block(index_offset, index_size)):
        offset, pos = varint(handle)
        size, _ = varint(handle, pos)
        for key, value in block_entries(block(offset, size)):
            if len(key) < 8:
                raise ValueError("Invalid LevelDB internal key")
            tag = struct.unpack_from("<Q", key, len(key) - 8)[0]
            yield key[:-8], tag >> 8, tag & 255, value


def active_files(manifest):
    tables, log_number, previous_log = {}, None, 0
    for record in log_records(manifest):
        pos = 0
        while pos < len(record):
            tag, pos = varint(record, pos)
            if tag == 1:
                _, pos = blob(record, pos)
            elif tag in (2, 3, 4, 9):
                value, pos = varint(record, pos)
                if tag == 2:
                    log_number = value
                elif tag == 9:
                    previous_log = value
            elif tag == 5:
                _, pos = varint(record, pos)
                _, pos = blob(record, pos)
            elif tag in (6, 7):
                level, pos = varint(record, pos)
                number, pos = varint(record, pos)
                if tag == 6:
                    tables.pop((level, number), None)
                else:
                    _, pos = varint(record, pos)
                    _, pos = blob(record, pos)
                    _, pos = blob(record, pos)
                    tables[(level, number)] = number
            else:
                raise ValueError(f"Unsupported LevelDB manifest tag {tag}")
    if log_number is None:
        raise ValueError("LevelDB manifest has no log number")
    return sorted(set(tables.values())), {n for n in (log_number, previous_log) if n}


def batch_entries(data):
    if len(data) < 12:
        raise ValueError("Truncated LevelDB write batch")
    sequence, count = struct.unpack_from("<QI", data)
    pos = 12
    for i in range(count):
        if pos >= len(data):
            raise ValueError("Truncated LevelDB batch entry")
        kind, pos = data[pos], pos + 1
        key, pos = blob(data, pos)
        if kind == 1:
            value, pos = blob(data, pos)
        elif kind == 0:
            value = b""
        else:
            raise ValueError("Unknown LevelDB batch entry type")
        yield key, sequence + i, kind, value
    if pos != len(data):
        raise ValueError("LevelDB batch trailing data")


def snapshot(path, *, key_filter=None):
    path = Path(path)
    captured = {}

    def read(name):
        file = path / name
        stat = file.stat()
        if stat.st_size > LIMIT:
            raise ValueError("LevelDB file exceeds the read limit")
        captured[name] = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        return file.read_bytes()

    current = read("CURRENT").decode().strip()
    if not current.startswith("MANIFEST-") or "/" in current or "\\" in current:
        raise ValueError("Invalid LevelDB CURRENT file")
    tables, logs = active_files(read(current))
    records = []
    for number in tables:
        name = f"{number:06}.ldb"
        if not (path / name).exists():
            name = f"{number:06}.sst"
        records.extend(row for row in table_entries(read(name)) if key_filter is None or key_filter(row[0]))
    for number in sorted(logs):
        for batch in log_records(read(f"{number:06}.log")):
            records.extend(row for row in batch_entries(batch) if key_filter is None or key_filter(row[0]))
    for name, fingerprint in captured.items():
        stat = (path / name).stat()
        if (stat.st_ino, stat.st_size, stat.st_mtime_ns) != fingerprint:
            raise ValueError("Firestore cache changed while reading; close Arc and retry")
    latest = {}
    for key, sequence, kind, value in records:
        if kind not in (0, 1):
            raise ValueError("Unsupported LevelDB value type")
        if key not in latest or sequence > latest[key][0]:
            latest[key] = (sequence, kind, value)
    return {key: value for key, (_, kind, value) in latest.items() if kind == 1}
