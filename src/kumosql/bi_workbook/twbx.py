"""Replace the ``.twb`` inside a packaged workbook (``.twbx``) and keep every other member byte for byte.

A ``.twbx`` is a zip archive: the workbook XML plus extracts (``Data/...hyper``), images and other files. Python's
``zipfile`` can only write a member by compressing it again, which changes the stored bytes of every extract. This module
instead copies each other member's local header, compressed data and central directory record as they are, writes the
new workbook as one deflated member, and fixes the offsets. :func:`replace_member` checks the result by reading it back
with ``zipfile``; an archive it cannot rewrite exactly (zip64, encryption, split archives) is refused with
:class:`PackageError`, and nothing is written.
"""

from __future__ import annotations

import io
import struct
import zipfile
import zlib

__all__ = ["PackageError", "replace_member"]

_EOCD = b"PK\x05\x06"
_ZIP64_LOCATOR = b"PK\x06\x07"
_CENTRAL = b"PK\x01\x02"
_LOCAL = b"PK\x03\x04"
_DESCRIPTOR = b"PK\x07\x08"
_CENTRAL_FMT = "<4sHHHHHHIIIHHHHHII"  # 46 bytes
_LOCAL_FMT = "<4sHHHHHIIIHH"  # 30 bytes
_EOCD_FMT = "<4sHHHHIIH"  # 22 bytes


class PackageError(ValueError):
    """The packaged workbook cannot be rewritten safely; the message says why."""


def _eocd(data: bytes) -> tuple[int, tuple]:
    start = max(0, len(data) - (22 + 0xFFFF))
    position = data.rfind(_EOCD, start)
    while position >= 0:
        fields = struct.unpack_from(_EOCD_FMT, data, position)
        if position + 22 + fields[7] == len(data):
            return position, fields
        position = data.rfind(_EOCD, start, position)
    raise PackageError("no end-of-archive record: not a zip archive, or one with trailing data")


def _records(data: bytes) -> tuple[list[dict], tuple, int]:
    position, eocd = _eocd(data)
    _, disk, cd_disk, on_disk, total, cd_size, cd_offset, _ = eocd
    if position >= 20 and data[position - 20:position - 16] == _ZIP64_LOCATOR:
        raise PackageError("the archive uses zip64 records, which this rewriter does not handle")
    if disk or cd_disk or on_disk != total:
        raise PackageError("the archive is split across several files")
    if 0xFFFF in (on_disk, total) or 0xFFFFFFFF in (cd_size, cd_offset):
        raise PackageError("the archive needs zip64, which this rewriter does not handle")
    if cd_offset + cd_size != position:
        raise PackageError("the central directory does not end where the end-of-archive record starts")
    records = []
    cursor = cd_offset
    for _ in range(total):
        if data[cursor:cursor + 4] != _CENTRAL:
            raise PackageError("a central directory record is damaged")
        fields = struct.unpack_from(_CENTRAL_FMT, data, cursor)
        name_len, extra_len, comment_len = fields[10], fields[11], fields[12]
        size = 46 + name_len + extra_len + comment_len
        record = {
            "central": data[cursor:cursor + size], "flags": fields[3], "method": fields[4], "crc": fields[7],
            "csize": fields[8], "usize": fields[9], "offset": fields[16],
            "name": data[cursor + 46:cursor + 46 + name_len],
        }
        record["filename"] = record["name"].decode("utf-8" if record["flags"] & 0x800 else "cp437", "replace")
        if record["flags"] & 0x1:
            raise PackageError(f"member {record['filename']} is encrypted")
        if 0xFFFFFFFF in (record["csize"], record["usize"], record["offset"]):
            raise PackageError("the archive needs zip64, which this rewriter does not handle")
        local_at = record["offset"]
        if data[local_at:local_at + 4] != _LOCAL:
            raise PackageError(f"member {record['filename']} has no local header where the directory says")
        local = struct.unpack_from(_LOCAL_FMT, data, local_at)
        data_at = local_at + 30 + local[9] + local[10]
        end = data_at + record["csize"]
        if record["flags"] & 0x8:  # a data descriptor follows the data
            end += 16 if data[end:end + 4] == _DESCRIPTOR else 12
        if end > cd_offset:
            raise PackageError(f"member {record['filename']} runs into the central directory")
        record.update(local_at=local_at, data_at=data_at, end=end, local_header=data[local_at:data_at])
        records.append(record)
        cursor += size
    if cursor != position:
        raise PackageError("the central directory has bytes the records do not account for")
    return records, eocd, position


def replace_member(archive: bytes, member: str, content: bytes) -> bytes:
    """A copy of the zip ``archive`` in which member ``member`` holds ``content``; every other member is copied as stored."""

    records, eocd, eocd_at = _records(archive)
    targets = [r for r in records if r["filename"] == member]
    if len(targets) != 1:
        raise PackageError(f"the archive has {len(targets)} members named {member!r}")
    target = targets[0]
    method = target["method"]
    if method == zipfile.ZIP_DEFLATED:
        compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
        payload = compressor.compress(content) + compressor.flush()
    elif method == zipfile.ZIP_STORED:
        payload = content
    else:
        raise PackageError(f"the workbook member is compressed with method {method}, which this rewriter does not write")
    crc = zlib.crc32(content) & 0xFFFFFFFF
    out = bytearray()
    new_offsets: dict[int, int] = {}
    for record in sorted(records, key=lambda r: r["local_at"]):
        new_offsets[id(record)] = len(out)
        if record is target:
            header = bytearray(record["local_header"])
            fields = list(struct.unpack_from(_LOCAL_FMT, header, 0))
            fields[2] &= ~0x8  # sizes are in this header, so no data descriptor
            fields[6], fields[7], fields[8] = crc, len(payload), len(content)
            struct.pack_into(_LOCAL_FMT, header, 0, *fields)
            out += header
            out += payload
        else:
            out += archive[record["local_at"]:record["end"]]
    cd_offset = len(out)
    for record in records:
        central = bytearray(record["central"])
        fields = list(struct.unpack_from(_CENTRAL_FMT, central, 0))
        fields[16] = new_offsets[id(record)]
        if record is target:
            fields[3] &= ~0x8
            fields[7], fields[8], fields[9] = crc, len(payload), len(content)
        struct.pack_into(_CENTRAL_FMT, central, 0, *fields)
        out += central
    cd_size = len(out) - cd_offset
    if len(out) > 0xFFFFFFFF:
        raise PackageError("the rewritten archive would need zip64")
    tail = bytearray(archive[eocd_at:])
    fields = list(struct.unpack_from(_EOCD_FMT, tail, 0))
    fields[5], fields[6] = cd_size, cd_offset
    struct.pack_into(_EOCD_FMT, tail, 0, *fields)
    out += tail
    result = bytes(out)
    _check(archive, result, member, content)
    return result


def _check(before: bytes, after: bytes, member: str, content: bytes) -> None:
    """Read the new archive back: same members in the same order, the new workbook, every other member's bytes unchanged."""

    try:
        with zipfile.ZipFile(io.BytesIO(before)) as old, zipfile.ZipFile(io.BytesIO(after)) as new:
            if [i.filename for i in old.infolist()] != [i.filename for i in new.infolist()]:
                raise PackageError("the rewritten archive lists different members")
            if new.testzip() is not None:
                raise PackageError("the rewritten archive fails its own checksums")
            for info in new.infolist():
                expected = content if info.filename == member else old.read(info.filename)
                if new.read(info.filename) != expected:
                    raise PackageError(f"member {info.filename} reads back differently")
    except zipfile.BadZipFile as exc:
        raise PackageError(f"the rewritten archive does not open: {exc}") from exc
    old_records, _, _ = _records(before)
    new_records, _, _ = _records(after)
    for old, new in zip(old_records, new_records):
        if old["filename"] == member:
            continue
        if before[old["local_at"]:old["end"]] != after[new["local_at"]:new["end"]]:
            raise PackageError(f"member {old['filename']} would not be copied byte for byte")
