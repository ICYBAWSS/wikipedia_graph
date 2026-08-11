"""
Build a compact title-lookup index for instant client-side node->title resolution,
replacing the "#12345" placeholder / per-node DB query with an O(1) array lookup.

Sampled title lengths across 5 regions of the dataset (250k rows): consistently
~17-20 bytes/title average (~18.2 blended), so ~5.48M titles is ~100-110MB of raw
UTF-8 text, small next to the 901MB adjacency_csr.bin already shipped.

Source: wiki_graph_structure_full.db (nodes(id INTEGER, title TEXT)), same file
adjacency_csr.bin was cross-checked against for node count. Titles are static
per node id (unlike links, which grow as the live scrape continues), so this
source is safe for titles even though it lagged slightly on edges.

Output: titles.bin
  uint32 N                  node count (== layout N, 5,483,618)
  uint32[N+1] offsets       title i is bytes[offsets[i]:offsets[i+1]], UTF-8
  uint8[]     bytes         concatenated UTF-8 title text, no separators needed
"""
import struct
import time
import sqlite3
import os

SRC_DB = "wiki_graph_structure_full.db"
VIEWER_BIN = "viewer_full.bin"
OUT_PATH = "titles.bin"

t0 = time.time()

with open(VIEWER_BIN, "rb") as f:
    N = struct.unpack("<I", f.read(4))[0]
print(f"Layout N (node count) = {N:,}")

con = sqlite3.connect(SRC_DB)
con.execute("PRAGMA query_only = 1")
cur = con.execute("SELECT id, title FROM nodes WHERE id < ? ORDER BY id", (N,))

offsets = [0]
chunks = []
total_bytes = 0
count = 0
expected_id = 0
for node_id, title in cur:
    if node_id != expected_id:
        raise ValueError(f"gap or out-of-order id: expected {expected_id}, got {node_id}")
    b = (title or "").encode("utf-8")
    chunks.append(b)
    total_bytes += len(b)
    offsets.append(total_bytes)
    count += 1
    expected_id += 1
    if count % 1_000_000 == 0:
        print(f"  {count:,} titles read ({time.time()-t0:.1f}s elapsed)")
con.close()

assert count == N, f"expected {N:,} titles, got {count:,}"
print(f"Total: {count:,} titles, {total_bytes:,} bytes of text ({time.time()-t0:.1f}s elapsed)")

with open(OUT_PATH, "wb") as f:
    f.write(struct.pack("<I", N))
    f.write(struct.pack(f"<{N+1}I", *offsets))
    for b in chunks:
        f.write(b)

size_mb = os.path.getsize(OUT_PATH) / 1e6
print(f"Done in {time.time()-t0:.1f}s. {OUT_PATH}: {size_mb:.1f} MB")
