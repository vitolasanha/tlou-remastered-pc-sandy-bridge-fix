#!/usr/bin/env python3
"""Writes <out>/red-zone.bin for the loader's --red-zone (src/red_zone.c): HEADER, then the
G3RZONE1 section of scripts/red_zone.py. A table that matches the image is kept.

Usage: red_zone_table.py out/boot-linked.bin --out out [--isa auto|force|off]
Needs iced-x86: python -m pip install --no-cache-dir --target gow3_pc/out/pydeps iced-x86

The same table also carries the software replacements for the instructions an older CPU lacks
(BMI1, LZCNT, MOVBE, F16C; isa_emulation.py): --isa auto (the default; TLOU_ISA_REWRITE=auto|force|off)
replaces those the CPU does not have, force replaces all of them, off none. Exit status: 0 table written
(or kept), 1 no table (iced missing, invalid image), 3 the CPU needs the replacements and they were
not made.
"""
import argparse
import hashlib
import os
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'out' / 'pydeps'))
from prepare import span, unpack
import red_zone
import cpu_features

HEADER = struct.Struct('<8sQ32sQ')    # magic, image size, sha256, section bytes


def read_boot_linked(data):
    """Segments (address, size, flags) and image of a G3PROBE5 file, in link_modules.py's order."""
    magic, size, entry, ns, nr, ni, flags = unpack('<8s6Q', data, 0)
    if magic != b'G3PROBE5':
        raise ValueError('not a G3PROBE5 image')
    pos = 56 + 8 + 32   # header, procparam, main TLS
    nmodules, = unpack('<Q', data, pos)
    pos += 8 + nmodules * 56
    nbindings, = unpack('<Q', data, pos)
    pos += 8 + nbindings * 24
    segments = [unpack('<3Q', data, pos + i * 24) for i in range(ns)]
    pos += ns * 24 + ni * 128 + nr * 32
    return segments, span(data, pos, size)


def cached(path, digest, image_size, kinds=frozenset()):
    """Summary of a table that already matches, else None."""
    try:
        data = path.read_bytes()
        magic, size, sha, section_bytes = HEADER.unpack_from(data, 0)
        if magic != b'G3RZTAB1' or size != image_size or sha != digest or not section_bytes:
            return None
        section = data[HEADER.size:]
        magic, _, _, _, _, version, crc = red_zone.SECTION.unpack_from(section, 0)
        if magic != red_zone.MAGIC or version != red_zone.VERSION or crc != red_zone.source_crc(kinds):
            return None
        stats = dict(zip(red_zone.STATS_FIELDS, red_zone.STATS.unpack_from(section, red_zone.SECTION.size)))
        note = path.with_name('red-zone-isa.txt')
        extra = note.read_text(encoding='utf-8').strip() if kinds and note.is_file() else ''
        return red_zone.summary(stats) + ' (table kept)' + ('\n' + extra if extra else '')
    except (OSError, struct.error, ValueError):
        return None


def isa_kinds(mode):
    """(kinds to emulate, names of the missing sets) for --isa mode."""
    if mode == 'off':
        return frozenset(), []
    if mode == 'force':
        names = list(cpu_features.FEATURE_KINDS)
    else:
        names = cpu_features.missing_emulatable()
        if names is None:
            print('ISA emulation: the CPU could not be identified, none made (TLOU_ISA_REWRITE=force makes it)')
            return frozenset(), []
    return cpu_features.kinds_for(names), names


def main(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('boot', type=Path)
    p.add_argument('--out', type=Path, default=Path(__file__).resolve().parent.parent / 'out')
    p.add_argument('--isa', choices=('auto', 'force', 'off'), default=os.environ.get('TLOU_ISA_REWRITE', 'auto').lower()
                   if os.environ.get('TLOU_ISA_REWRITE', 'auto').lower() in ('auto', 'force', 'off') else 'auto')
    p.add_argument('--isa-only', action='store_true', help='emulate the missing instructions, no red-zone protection')
    a = p.parse_args(argv)
    kinds, names = isa_kinds(a.isa)
    key = kinds | {'isa-only'} if a.isa_only else kinds
    if names:
        print(f"ISA emulation: this CPU lacks {', '.join(names)}: those instructions are replaced by code")
    try:
        if red_zone.iced_x86 is None:
            raise RuntimeError('iced_x86 is not installed')
        segments, image = read_boot_linked(a.boot.read_bytes())
        digest = hashlib.sha256(image).digest()
        table = a.out / 'red-zone.bin'
        kept = cached(table, digest, len(image), key)
        if kept:
            print(kept)
            return 0
        builder, stats = red_zone.analyze(image, segments, red_zone.code_offset_for(len(image), 0), isa_kinds=kinds, isa_only=a.isa_only)
        section = red_zone.encode_section(builder, stats, key)
        table.write_bytes(HEADER.pack(b'G3RZTAB1', len(image), digest, len(section)) + section)
        note = table.with_name('red-zone-isa.txt')
        text = red_zone.isa_summary(builder)
        if text:
            note.write_text(text + '\n', encoding='utf-8')
        else:
            note.unlink(missing_ok=True)
    except (OSError, ValueError, RuntimeError) as e:
        print(f'Red zone: protection unavailable ({e})')
        return 3 if kinds else 1
    print(red_zone.summary(stats))
    if text:
        print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
