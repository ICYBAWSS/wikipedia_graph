"""
Merge contexts.db (correct, freshly-derived link contexts) into a working copy
of wiki_simulation.db's `links.context` column.

SAFETY: this script only ever writes to --main, which the caller must point at
a *copy* of the live DB, never the original file. The original artifact on HF
is left completely untouched -- the merged result is uploaded under a new
filename by the calling shell script, so rollback is reverting one URL in
engine.js, not restoring a mutated file.

Merge logic is validated (see scratchpad/test_merge_sql.py, run locally before
this was ever pointed at real data):
  - rows with no match in contexts.db are left byte-identical (NULL stays NULL,
    an existing value stays as-is)
  - rows with a match get their context overwritten, even if they already had
    one from the old buggy matcher
  - row COUNT of `links` is asserted unchanged before/after -- this only ever
    updates in place, never inserts/deletes
"""
import argparse
import sqlite3
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--main", required=True, help="writable copy of wiki_simulation.db")
    ap.add_argument("--contexts", required=True, help="contexts.db from rebuild_contexts.py")
    args = ap.parse_args()

    t0 = time.time()
    con = sqlite3.connect(args.main)
    con.execute("PRAGMA journal_mode = OFF")
    con.execute("PRAGMA synchronous = OFF")
    con.execute("PRAGMA cache_size = -2000000")  # ~2GB page cache, plenty of RAM here

    before = con.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    print(f"main links table: {before:,} rows", flush=True)

    con.execute(f"ATTACH DATABASE '{args.contexts}' AS ctxdb")
    ctx_rows = con.execute("SELECT COUNT(*) FROM ctxdb.links").fetchone()[0]
    print(f"contexts db: {ctx_rows:,} candidate edges", flush=True)

    # contexts.db ships with only idx_ctx_src (source_idx alone, see
    # rebuild_contexts.py) -- add the compound index the merge actually needs.
    # Building it is one sequential index build over 66M rows, not per-row work.
    print("building compound index on contexts.db(source_idx, target_idx) ...", flush=True)
    t1 = time.time()
    con.execute("CREATE INDEX IF NOT EXISTS ctxdb.idx_ctx_pair ON links(source_idx, target_idx)")
    print(f"  done in {time.time()-t1:.1f}s", flush=True)

    # The main table has single-column indices only (idx_links_src_idx /
    # idx_links_tgt_idx) -- neither helps an (source_idx, target_idx) match.
    # Add the compound index once; the project's own build_adjacency_csr.py
    # notes the same lesson (avoid per-row lookups against a single-column
    # index on a huge table -- sequential/indexed set operations instead).
    print("building compound index on main links(source_idx, target_idx) ...", flush=True)
    t1 = time.time()
    con.execute("CREATE INDEX IF NOT EXISTS idx_links_pair ON links(source_idx, target_idx)")
    print(f"  done in {time.time()-t1:.1f}s", flush=True)

    print("running merge UPDATE ...", flush=True)
    t1 = time.time()
    cur = con.execute("""
        UPDATE links
        SET context = (
            SELECT c.context FROM ctxdb.links l
            JOIN ctxdb.contexts c ON c.ctx_id = l.ctx_id
            WHERE l.source_idx = links.source_idx AND l.target_idx = links.target_idx
        )
        WHERE EXISTS (
            SELECT 1 FROM ctxdb.links l2
            WHERE l2.source_idx = links.source_idx AND l2.target_idx = links.target_idx
        )
    """)
    con.commit()
    updated = cur.rowcount
    print(f"  {updated:,} rows updated in {time.time()-t1:.1f}s", flush=True)

    after = con.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    print(f"row count check: before={before:,} after={after:,}", flush=True)
    if before != after:
        print("!! ROW COUNT CHANGED -- merge must only UPDATE, never insert/delete. Aborting.",
              file=sys.stderr, flush=True)
        sys.exit(1)

    # Drop the compound index on the main table before shipping -- it wasn't
    # part of the original schema and roughly doubles reindex cost on the next
    # rebuild step for no benefit to the live viewer's query patterns.
    con.execute("DROP INDEX IF EXISTS idx_links_pair")
    con.execute("DETACH DATABASE ctxdb")
    print("VACUUM ...", flush=True)
    t1 = time.time()
    con.execute("VACUUM")
    print(f"  done in {time.time()-t1:.1f}s", flush=True)
    con.close()

    print(f"DONE in {(time.time()-t0)/60:.1f}m | {updated:,} edges corrected", flush=True)


if __name__ == "__main__":
    main()
