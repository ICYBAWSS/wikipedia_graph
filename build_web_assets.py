#!/usr/bin/env python3
"""
Repack the viewer's load-time binaries into compact, pre-gzipped v2 assets.

Why: the originals are float32-everything and served uncompressed (Hugging Face
sends .bin as application/octet-stream and ignores Accept-Encoding), so the
browser pulls 332 MB before it can draw a single pixel. Almost none of that
precision is real:

  * x/y span ~5736 units. uint16 over that span quantizes to 0.044 units, which
    is 0.06 px at 4K when the whole graph is on screen. Invisible.
  * `deg` is stored as float32 but has only ~4.4k distinct values across 6.9M
    nodes, so a uint16 palette index is exactly lossless.
  * `cat` is 0..6 in a float32.
  * every title is <= 253 bytes, so the (N+1) uint32 offset table can be a
    per-title uint8 length and be prefix-summed back at load.

Layout is planar (all x, then all y, ...) rather than interleaved because like
values sit next to like, which is what makes the gzip pass actually pay off.

Outputs (all gzip level 9) next to the inputs:
    viewer_v2.bin.gz
    edgeTgt_v2.bin.gz
    titles_v2.bin.gz

The originals are left untouched and stay on HF -- engine.js falls back to them
when DecompressionStream is missing or a v2 asset 404s.
"""
import gzip
import struct
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
GZIP_LEVEL = 9

# Bumped whenever a format below changes shape, so a stale cached asset can
# never be silently misread as a current one. Must match ASSET_V in engine.js.
MAGIC_VIEWER = b"WGV2"
MAGIC_EDGE = b"WGE2"
MAGIC_TITLES = b"WGT2"


def _write_gz(path: Path, payload: bytes, original_size: int) -> None:
    t0 = time.time()
    blob = gzip.compress(payload, GZIP_LEVEL)
    path.write_bytes(blob)
    print(
        f"  -> {path.name}: {original_size / 1e6:.1f} MB "
        f"-> {len(payload) / 1e6:.1f} MB raw "
        f"-> {len(blob) / 1e6:.1f} MB gz "
        f"({original_size / len(blob):.2f}x total, {time.time() - t0:.0f}s)"
    )


def load_viewer(path: Path):
    with path.open("rb") as f:
        n = struct.unpack("I", f.read(4))[0]
        raw = np.fromfile(f, dtype=np.float32, count=n * 4).reshape(n, 4)
    return n, raw[:, 0], raw[:, 1], raw[:, 2], raw[:, 3]


def build_viewer(n, x, y, deg, cat, lo, scale):
    """Header, then palette, then planar uint16/uint8 columns.

    Field order is chosen so every typed-array view lands on its natural
    alignment when the decompressed buffer is read back in JS: the 24-byte
    header keeps the float32 palette 4-aligned, and 24 + 4*P is always even so
    the uint16 columns that follow it are 2-aligned.
    """
    xq = np.round((x - lo) * scale).astype(np.uint16)
    yq = np.round((y - lo) * scale).astype(np.uint16)

    palette, deg_idx = np.unique(deg, return_inverse=True)
    if len(palette) > 65536:
        sys.exit(f"degree palette too large for uint16: {len(palette)}")
    deg_idx = deg_idx.astype(np.uint16)

    if cat.max() > 255:
        sys.exit(f"category id too large for uint8: {cat.max()}")

    header = (
        MAGIC_VIEWER
        + struct.pack("<I", n)
        + struct.pack("<I", len(palette))
        + struct.pack("<f", lo)
        + struct.pack("<f", scale)
        + b"\0\0\0\0"  # pad to 24 so the palette below stays 4-aligned
    )
    assert len(header) == 24, len(header)

    # Report the worst-case round-trip error so a bad scale can't pass silently.
    err = np.abs((xq.astype(np.float32) / scale + lo) - x).max()
    print(f"  max coordinate round-trip error: {err:.4f} units")

    return header + b"".join(
        (
            palette.astype(np.float32).tobytes(),
            xq.tobytes(),
            yq.tobytes(),
            deg_idx.tobytes(),
            cat.astype(np.uint8).tobytes(),
        )
    )


def build_edge(path: Path, n, lo, scale):
    """Each node's strongest-neighbour position, quantized on the same grid.

    Same lo/scale as the node coordinates on purpose: a neighbour position and
    the node actually sitting there then quantize to the identical pair, so the
    hairline edges keep landing exactly on their endpoints. NaN (no neighbour)
    is carried as a 1-bit-per-node mask rather than a sentinel coordinate,
    since every uint16 pair is a legitimate position.
    """
    with path.open("rb") as f:
        file_n = struct.unpack("I", f.read(4))[0]
        et = np.fromfile(f, dtype=np.float32, count=file_n * 2).reshape(file_n, 2)
    if file_n != n:
        sys.exit(f"edgeTgt node count {file_n} != viewer node count {n}")

    tx, ty = et[:, 0], et[:, 1]
    has = np.isfinite(tx) & np.isfinite(ty)
    print(f"  nodes with a strongest neighbour: {has.sum():,} / {n:,}")

    safe_x = np.where(has, tx, lo)
    safe_y = np.where(has, ty, lo)
    txq = np.round(np.clip((safe_x - lo) * scale, 0, 65535)).astype(np.uint16)
    tyq = np.round(np.clip((safe_y - lo) * scale, 0, 65535)).astype(np.uint16)

    # The mask is ceil(n/8) bytes, which is odd whenever n/8 lands just past a
    # whole byte (true here: 6,919,752 / 8 is exact, but that won't hold for
    # every future N) -- pad it to even so txq/tyq start at a uint16-aligned
    # offset. JS recomputes this identical padding from n alone, no header field
    # needed for it.
    # np.packbits defaults to bitorder='big': element 0 of `has` lands in the
    # MSB (0x80) of byte 0, not the LSB. The JS reader has to unpack in that
    # same order -- a mismatch here doesn't error, it just quietly reads a
    # different node's bit than intended (see inflateEdgeV2 in engine.js).
    mask = np.packbits(has).tobytes()
    if len(mask) % 2:
        mask += b"\0"

    header = MAGIC_EDGE + struct.pack("<I", n)
    return header + b"".join((mask, txq.tobytes(), tyq.tobytes()))


def build_titles(path: Path, n):
    with path.open("rb") as f:
        file_n = struct.unpack("I", f.read(4))[0]
        offsets = np.fromfile(f, dtype=np.uint32, count=file_n + 1)
        text = f.read()
    if file_n != n:
        sys.exit(f"titles node count {file_n} != viewer node count {n}")

    lengths = np.diff(offsets)
    if lengths.max() > 255:
        sys.exit(f"title too long for uint8 length: {lengths.max()} bytes")
    print(f"  longest title: {lengths.max()} bytes; text {len(text) / 1e6:.1f} MB")

    header = MAGIC_TITLES + struct.pack("<I", n)
    return header + lengths.astype(np.uint8).tobytes() + text


def build_csr_gz(path: Path) -> None:
    """Plain gzip of a CSR file, byte-for-byte identical layout inside -- no
    quantization here (the values are already-minimal uint32 node indices, not
    floats with headroom to cut), just compression. Node indices don't run/repeat
    the way coordinates or degrees do, so the ratio is a modest ~1.4x rather than
    the 3x+ the other v2 assets get -- still worth it at this size. Level 6, not
    9: these files are ~360MB each and 9 buys negligible extra ratio for a lot
    more CPU time.
    """
    t0 = time.time()
    raw = path.read_bytes()
    blob = gzip.compress(raw, 6)
    out = path.with_suffix(path.suffix + ".gz")
    out.write_bytes(blob)
    print(f"  -> {out.name}: {len(raw) / 1e6:.1f} MB -> {len(blob) / 1e6:.1f} MB gz "
          f"({len(raw) / len(blob):.2f}x, {time.time() - t0:.0f}s)")


def main():
    viewer_path = ROOT / "viewer_full.bin"
    edge_path = ROOT / "edgeTgt.bin"
    titles_path = ROOT / "titles.bin"
    for p in (viewer_path, edge_path, titles_path):
        if not p.exists():
            sys.exit(f"missing input: {p}")

    print("reading viewer_full.bin ...")
    n, x, y, deg, cat = load_viewer(viewer_path)
    print(f"  N = {n:,}")

    # One shared quantization grid for node and edge-target coordinates, padded
    # slightly past the true extent so rounding can never land outside uint16.
    lo = float(min(x.min(), y.min()))
    hi = float(max(x.max(), y.max()))
    pad = (hi - lo) * 1e-4
    lo -= pad
    hi += pad
    scale = 65535.0 / (hi - lo)
    print(f"  quantization grid: lo={lo:.3f} scale={scale:.6f}")

    print("building viewer_v2 ...")
    _write_gz(ROOT / "viewer_v2.bin.gz", build_viewer(n, x, y, deg, cat, lo, scale),
              viewer_path.stat().st_size)

    print("building edgeTgt_v2 ...")
    _write_gz(ROOT / "edgeTgt_v2.bin.gz", build_edge(edge_path, n, lo, scale),
              edge_path.stat().st_size)

    print("building titles_v2 ...")
    _write_gz(ROOT / "titles_v2.bin.gz", build_titles(titles_path, n),
              titles_path.stat().st_size)

    for csr_name in ("adjacency_csr.bin", "adjacency_csr_rev.bin"):
        csr_path = ROOT / csr_name
        if csr_path.exists():
            print(f"gzipping {csr_name} ...")
            build_csr_gz(csr_path)
        else:
            print(f"skipping {csr_name} (not present)")

    print("\ndone.")


if __name__ == "__main__":
    main()
