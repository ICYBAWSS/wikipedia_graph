"""
Rebuild link contexts from the pinned enwiki dump, using the corrected
sentence-matching logic in scraper/xml_parser.py.

WHY: the original contexts were built by scanning every sentence in an article
for the link's anchor text as a plain substring, with no check that the sentence
actually contained that link's [[...]] markup. A page linking [[India]] in one
section but saying "Indian-American" as plain prose in the intro would store the
intro sentence as India's context -- text where India is not a link at all. This
recomputes every context, only ever taking the sentence holding the link itself.

Emits a slim contexts-only DB rather than rebuilding the full 25GB pipeline:
  contexts.db  links(source_idx INTEGER, target_idx INTEGER, context TEXT)
Indices are node ids == `rowid - 1` of the nodes table == titles.bin position
(verified identical for sampled titles), so this drops straight into the
existing links/CSR index space.

Parsing runs in a worker pool (regex over ~5.5M articles is the bottleneck);
title->index mapping and SQLite writes stay in the parent so the 5.5M-entry
dict exists once.
"""
import argparse
import bz2
import os
import re
import sqlite3
import sys
import time
from multiprocessing import Pool

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scraper"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The fixed parser. Kept as the single source of truth -- do not inline a copy.
from xml_parser import parse_wikitext_links  # noqa: E402

PAGE_RE = re.compile(r"<page>(.*?)</page>", re.DOTALL)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
TEXT_RE = re.compile(r"<text[^>]*>(.*?)</text>", re.DOTALL)
NS_RE = re.compile(r"<ns>(\d+)</ns>")


def unescape(s):
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&#039;", "'").replace("&amp;", "&"))


def parse_page(block):
    """Worker: raw <page> body -> (title, {target: context}) or None."""
    nm = NS_RE.search(block)
    if not nm or nm.group(1) != "0":
        return None
    tm = TITLE_RE.search(block)
    xm = TEXT_RE.search(block)
    if not (tm and xm):
        return None
    body = xm.group(1)
    if body.strip().upper().startswith("#REDIRECT"):
        return None
    title = unescape(tm.group(1))
    _links, contexts = parse_wikitext_links(unescape(body))
    if not contexts:
        return None
    return (title, contexts)


def _raw_chunks(src):
    """Yield decompressed text chunks from a local path or an http(s) URL.

    Streaming matters: RunPod CPU pods cap disk at ~30GB, which cannot hold the
    24.3GB dump *and* the ~11GB output. The dump is read once, front to back, so
    we never store it -- decompress it off the socket instead. BZ2Decompressor is
    driven manually (rather than bz2.open) because the dump is *multistream*:
    many concatenated bz2 streams, each ending with eof, and unused_data carries
    the head of the next one.
    """
    if src.startswith("http://") or src.startswith("https://"):
        import urllib.request
        req = urllib.request.Request(src, headers={"User-Agent": "wiki-ctx-rebuild/1.0"})
        fh = urllib.request.urlopen(req, timeout=180)
        reader = lambda: fh.read(4 * 1024 * 1024)  # noqa: E731
    else:
        fh = open(src, "rb")
        reader = lambda: fh.read(4 * 1024 * 1024)  # noqa: E731

    dec = bz2.BZ2Decompressor()
    try:
        while True:
            raw = reader()
            if not raw:
                break
            while raw:
                out = dec.decompress(raw)
                if out:
                    yield out.decode("utf-8", errors="ignore")
                if dec.eof:
                    raw = dec.unused_data      # next concatenated stream
                    dec = bz2.BZ2Decompressor()
                else:
                    raw = b""
    finally:
        fh.close()


def iter_pages(src):
    """Yield raw <page>...</page> bodies from a (multistream) bz2 dump."""
    buf = ""
    for chunk in _raw_chunks(src):
        buf += chunk
        last = 0
        for m in PAGE_RE.finditer(buf):
            yield m.group(1)
            last = m.end()
        if last:
            buf = buf[last:]
        # Guard against a pathological run with no closing tag.
        if len(buf) > 64 * 1024 * 1024:
            buf = buf[-8 * 1024 * 1024:]


LAYOUT_N = 5_483_618  # node count of the RunPod layout; see build_adjacency_csr.py


def load_title_index(structure_db, n=LAYOUT_N):
    """title -> node index.

    Mirrors build_titles_index.py exactly: in wiki_graph_structure.db the nodes
    table carries an explicit `id` INTEGER that IS the node index (contiguous
    0..N-1), and `title`. Do not substitute rowid-1 here -- that only coincides
    with id as long as the table has no gaps, and this asserts contiguity the
    same way the titles.bin builder does.
    """
    print(f"Loading title index from {structure_db} (N={n:,}) ...", flush=True)
    con = sqlite3.connect(structure_db)
    con.execute("PRAGMA query_only = 1")
    cols = [r[1] for r in con.execute("PRAGMA table_info(nodes)")]
    print(f"  nodes columns: {cols}", flush=True)

    idx = {}
    expected = 0
    for node_id, title in con.execute(
            "SELECT id, title FROM nodes WHERE id < ? ORDER BY id", (n,)):
        if node_id != expected:
            raise ValueError(f"gap/out-of-order node id: expected {expected}, got {node_id}")
        if title is not None:
            idx[title] = node_id
        expected += 1
    con.close()

    if expected != n:
        raise ValueError(f"expected {n:,} nodes, got {expected:,}")
    print(f"  {len(idx):,} titles indexed", flush=True)
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--structure-db", required=True)
    ap.add_argument("--out", default="contexts.db")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument("--limit", type=int, default=0, help="stop after N articles (smoke test)")
    args = ap.parse_args()

    # Fork the workers FIRST, while this process is still small. Workers only
    # parse text and never touch the title map, so building the ~1GB dict before
    # forking would hand every child a copy-on-write copy that Python's
    # refcounting then gradually makes real -- fatal on a 4GB CPU pod.
    pool = Pool(args.workers)

    title_idx = load_title_index(args.structure_db)

    if os.path.exists(args.out):
        os.remove(args.out)
    out = sqlite3.connect(args.out)
    out.execute("PRAGMA journal_mode = OFF")
    out.execute("PRAGMA synchronous = OFF")
    out.execute("PRAGMA cache_size = -50000")
    # Contexts are stored once and referenced, not repeated per edge. The matcher
    # gives every link in a sentence that same sentence, so a flat
    # (src,tgt,context) table repeats each sentence once per link it contains --
    # measured ~2-3x redundancy. Splitting the text out keeps the artifact small
    # enough to build on a 30GB CPU pod, and cheaper to ship and serve.
    out.execute("CREATE TABLE contexts (ctx_id INTEGER PRIMARY KEY, context TEXT)")
    out.execute("CREATE TABLE links (source_idx INTEGER, target_idx INTEGER, ctx_id INTEGER)")

    t0 = time.time()
    pages = arts = rows = skipped_src = 0
    next_ctx_id = 0
    link_batch = []
    ctx_batch = []

    try:
        for res in pool.imap_unordered(parse_page, iter_pages(args.dump), chunksize=64):
            pages += 1
            if res is None:
                continue
            title, contexts = res
            src = title_idx.get(title)
            if src is None:
                skipped_src += 1
                continue
            arts += 1
            # Dedup within the article: one sentence typically carries several of
            # this article's links, and cross-article repeats are rare enough that
            # a global map would cost far more memory than it saves.
            local = {}
            for target, ctx in contexts.items():
                tgt = title_idx.get(target)
                if tgt is None:
                    continue  # link leaves the graph's node set
                cid = local.get(ctx)
                if cid is None:
                    cid = next_ctx_id
                    next_ctx_id += 1
                    local[ctx] = cid
                    ctx_batch.append((cid, ctx))
                link_batch.append((src, tgt, cid))

            if len(link_batch) >= 100_000:
                out.executemany("INSERT INTO contexts VALUES (?,?)", ctx_batch)
                out.executemany("INSERT INTO links VALUES (?,?,?)", link_batch)
                out.commit()
                rows += len(link_batch)
                link_batch.clear()
                ctx_batch.clear()
                el = time.time() - t0
                print(f"  pages={pages:,} articles={arts:,} rows={rows:,} "
                      f"ctx={next_ctx_id:,} ({el/60:.1f}m, {pages/max(el,1):.0f} pages/s)",
                      flush=True)

            if args.limit and arts >= args.limit:
                break
    finally:
        pool.terminate()
        pool.join()

    if link_batch:
        out.executemany("INSERT INTO contexts VALUES (?,?)", ctx_batch)
        out.executemany("INSERT INTO links VALUES (?,?,?)", link_batch)
        out.commit()
        rows += len(link_batch)

    print(f"Indexing {rows:,} rows ...", flush=True)
    out.execute("CREATE INDEX idx_ctx_src ON links(source_idx)")
    out.commit()
    out.close()

    el = time.time() - t0
    size = os.path.getsize(args.out)
    print(f"DONE in {el/60:.1f}m | pages={pages:,} articles={arts:,} rows={rows:,} "
          f"| title-miss={skipped_src:,} | {args.out} = {size/1e9:.2f} GB", flush=True)


if __name__ == "__main__":
    main()
