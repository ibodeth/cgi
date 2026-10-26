"""In-place ISO-BMFF (MP4/MOV) sample-entry sanitiser.

FFmpeg's MOV muxer hard-codes the ``FFMP`` vendor 4CC in QuickTime video
sample descriptions and copies the ``Lavc ... libx264`` encoder string into the
32-byte ``compressorname`` field. FFmpeg's ``-vendor`` AVOption only exists on
the ProRes encoder, so it cannot reach these fields for H.264/HEVC. Both fields
have a fixed size, so they are rewritten in place without touching any box
sizes or sample offsets.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

_STBL_PATH = (b"mdia", b"minf", b"stbl")
_VISUAL_ENTRIES = {b"avc1", b"avc3", b"hvc1", b"hev1"}

# Offsets inside a VisualSampleEntry, relative to the start of its box header.
_VENDOR_OFFSET = 20
_COMPRESSOR_OFFSET = 50
_COMPRESSOR_SIZE = 32
_MIN_ENTRY_SIZE = 86


class ContainerPatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Box:
    type: bytes
    offset: int
    """Absolute file offset of the box header."""
    header_size: int
    size: int

    @property
    def payload_offset(self) -> int:
        return self.offset + self.header_size

    @property
    def end(self) -> int:
        return self.offset + self.size


def _read_header(fh: BinaryIO, offset: int, limit: int) -> Box | None:
    if limit - offset < 8:
        return None
    fh.seek(offset)
    raw = fh.read(8)
    if len(raw) < 8:
        return None
    size, box_type = struct.unpack(">I4s", raw)
    header = 8
    if size == 1:
        large = fh.read(8)
        if len(large) < 8:
            return None
        size = struct.unpack(">Q", large)[0]
        header = 16
    elif size == 0:
        size = limit - offset
    if size < header or offset + size > limit:
        raise ContainerPatchError(f"corrupt box '{box_type!r}' at offset {offset}")
    return Box(box_type, offset, header, size)


def _children(fh: BinaryIO, start: int, end: int) -> Iterator[Box]:
    offset = start
    while True:
        box = _read_header(fh, offset, end)
        if box is None:
            return
        yield box
        offset = box.end


def _find_sample_entries(fh: BinaryIO, file_size: int) -> Iterator[Box]:
    for top in _children(fh, 0, file_size):
        if top.type != b"moov":
            continue
        for trak in _children(fh, top.payload_offset, top.end):
            if trak.type != b"trak":
                continue
            node = trak
            for wanted in _STBL_PATH:
                found = next((b for b in _children(fh, node.payload_offset, node.end) if b.type == wanted), None)
                if found is None:
                    break
                node = found
            else:
                stsd = next((b for b in _children(fh, node.payload_offset, node.end) if b.type == b"stsd"), None)
                if stsd is None:
                    continue
                # stsd is a FullBox: version/flags (4) + entry_count (4).
                yield from _children(fh, stsd.payload_offset + 8, stsd.end)


def sanitize_sample_entries(path: Path, vendor: str | None) -> int:
    """Blank the compressor name and set the vendor 4CC of H.264/HEVC sample entries.

    ``vendor`` is written only for QuickTime (.mov) files; pass ``None`` for MP4,
    where ISO/IEC 14496-12 requires that field to stay zero. Returns the number
    of sample entries patched.
    """
    vendor_bytes = vendor.encode("ascii").ljust(4, b"\0")[:4] if vendor is not None else None
    patched = 0
    with path.open("r+b") as fh:
        fh.seek(0, 2)
        file_size = fh.tell()
        entries = [e for e in _find_sample_entries(fh, file_size) if e.type in _VISUAL_ENTRIES]
        for entry in entries:
            if entry.size < _MIN_ENTRY_SIZE or entry.header_size != 8:
                continue
            if vendor_bytes is not None:
                fh.seek(entry.offset + _VENDOR_OFFSET)
                fh.write(vendor_bytes)
            fh.seek(entry.offset + _COMPRESSOR_OFFSET)
            fh.write(b"\0" * _COMPRESSOR_SIZE)
            patched += 1
    return patched
