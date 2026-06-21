import os
import json
import sqlite3
import re

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "wiki_cache.db"))

# Blacklist prefixes/keywords for administrative categories
CAT_BLACKLIST_PREFIXES = (
    "Category:All ", "Category:Articles ", "Category:Use ", "Category:Short description",
    "Category:Pages ", "Category:Wikipedia ", "Category:CS1 ", "Category:Webarchive ",
    "Category:Template ", "Category:Good articles", "Category:Featured articles",
    "Category:Spoken articles", "Category:Link ", "Category:Tracked ", "Category:Identifiers ",
    "Category:ACAD ", "Category:VIAF ", "Category:LCCN ", "Category:GND ", "Category:Sudoc ",
    "Category:BNF "
)

# Categorization rules
TOPICS = {
    "Biography & People": ["births", "deaths", "people", "family", "royalty", "biography", "leader", "singer", "actor", "writer", "author"],
    "Science & Technology": ["science", "physics", "biology", "mathematics", "computer", "software", "technology", "space", "medicine"],
    "History & Society": ["history", "election", "war", "military", "politics", "empire", "government", "sociology", "economy"],
    "Art & Culture": ["art", "music", "film", "television", "entertainment", "sport", "game", "literature", "culture", "museum"],
    "Philosophy & Religion": ["philosophy", "religion", "myth", "theology", "god", "belief", "buddhism", "christianity", "islam"],
    "Geography & Places": ["cities", "countries", "mountains", "rivers", "islands", "geography", "towns", "villages", "regions"]
}

def clean_categories(categories_json):
    try:
        cats = json.loads(categories_json)
        return [c for c in cats if not any(c.startswith(p) for p in CAT_BLACKLIST_PREFIXES)]
    except: return []

def classify_topic(title, categories, wd_type):
    combined_text = (title + " " + " ".join(categories)).lower()
    scores = {topic: 0 for topic in TOPICS}
    for topic, keywords in TOPICS.items():
        for keyword in keywords:
            if keyword in combined_text:
                scores[topic] += (combined_text.count(keyword) + (5 if keyword in title.lower() else 0))
    best_topic = max(scores, key=scores.get)
    return best_topic if scores[best_topic] > 0 else "Other & General"

def compile_graph():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    print("Step 1: Pre-scanning valid titles...")
    cursor.execute("SELECT title FROM articles WHERE crawled = 1")
    valid_titles = {row[0] for row in cursor.fetchall()}
    print(f"Found {len(valid_titles)} valid articles.")

    db_out_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "wiki_graph.db"))
    if os.path.exists(db_out_path): os.remove(db_out_path)
    out_conn = sqlite3.connect(db_out_path)
    out_cursor = out_conn.cursor()
    out_cursor.execute("PRAGMA journal_mode = OFF")
    out_cursor.execute("PRAGMA synchronous = OFF")
    out_cursor.execute("PRAGMA cache_size = -4000000") # 4GB cache for output DB

    out_cursor.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, category TEXT, views INTEGER,
            inDegree INTEGER, outDegree INTEGER, x REAL, y REAL, snippet TEXT
        )
    """)
    out_cursor.execute("""
        CREATE TABLE links (
            source TEXT, target TEXT, source_idx INTEGER, target_idx INTEGER, context TEXT
        )
    """)

    print("Step 2: Building node metadata...")
    cursor.execute("SELECT title, snippet, views, categories, wikidata_type FROM articles WHERE crawled = 1")
    nodes_metadata = []
    while True:
        row = cursor.fetchone()
        if not row: break
        title, snippet, views, categories, wd_type = row
        nodes_metadata.append({
            "id": title, "snippet": snippet, "views": views or 0,
            "categories": categories, "wd_type": wd_type
        })
    
    nodes_metadata.sort(key=lambda x: x["id"])
    title_to_idx = {n["id"]: i for i, n in enumerate(nodes_metadata)}
    
    in_degrees = {n["id"]: 0 for n in nodes_metadata}
    out_degrees = {n["id"]: 0 for n in nodes_metadata}

    print("Step 3: Processing links...")
    cursor.execute("SELECT title, links, link_contexts FROM articles WHERE crawled = 1")
    link_batch = []
    row_count = 0
    while True:
        row = cursor.fetchone()
        if not row: break
        source, links_raw, link_contexts_raw = row
        try:
            links_list = json.loads(links_raw) if links_raw else []
            contexts_map = json.loads(link_contexts_raw) if link_contexts_raw else {}
        except: continue

        for target in links_list:
            if target in valid_titles:
                s_idx, t_idx = title_to_idx[source], title_to_idx[target]
                link_batch.append((source, target, s_idx, t_idx, contexts_map.get(target, "")))
                out_degrees[source] += 1
                in_degrees[target] += 1
                if len(link_batch) >= 100000:
                    out_cursor.executemany("INSERT INTO links VALUES (?,?,?,?,?)", link_batch)
                    link_batch = []
        
        row_count += 1
        if row_count % 100000 == 0: print(f"  Processed {row_count} link sets...")

    if link_batch: out_cursor.executemany("INSERT INTO links VALUES (?,?,?,?,?)", link_batch)

    print("Step 4: Inserting nodes...")
    node_batch = []
    for n in nodes_metadata:
        category = classify_topic(n["id"], clean_categories(n["categories"]), n["wd_type"])
        node_batch.append((
            n["id"], category, n["views"], 
            in_degrees[n["id"]], out_degrees[n["id"]], 
            0.0, 0.0, n["snippet"]
        ))
        if len(node_batch) >= 50000:
            out_cursor.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?)", node_batch)
            node_batch = []
    if node_batch: out_cursor.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?)", node_batch)
    
    print("Step 5: Building final indexes...")
    out_cursor.execute("CREATE INDEX idx_nodes_xy ON nodes (x, y)")
    out_cursor.execute("CREATE INDEX idx_nodes_views ON nodes (views)")
    out_cursor.execute("CREATE INDEX idx_links_src_idx ON links (source_idx)")
    out_cursor.execute("CREATE INDEX idx_links_tgt_idx ON links (target_idx)")
    out_cursor.execute("CREATE VIRTUAL TABLE nodes_fts USING fts5(id, category, content='nodes', content_rowid='rowid')")
    out_cursor.execute("INSERT INTO nodes_fts(rowid, id, category) SELECT rowid, id, category FROM nodes")
    
    out_conn.commit()

    print("Step 6: Exporting binary coordinates...")
    import struct
    bin_path = os.path.join(os.path.dirname(db_out_path), "coordinates.bin")
    out_cursor.execute("SELECT x, y FROM nodes ORDER BY id")
    with open(bin_path, "wb") as f:
        f.write(struct.pack("I", len(nodes_metadata)))
        for x, y in out_cursor:
            f.write(struct.pack("ff", x, y))
    
    print("Step 7: Exporting top links...")
    top_links_path = os.path.join(os.path.dirname(db_out_path), "top_links.bin")
    out_cursor.execute("CREATE TEMP TABLE top_nodes AS SELECT (rowid - 1) AS idx FROM nodes ORDER BY views DESC LIMIT 100000")
    out_cursor.execute("CREATE INDEX temp_idx_top_nodes ON top_nodes(idx)")
    out_cursor.execute("""
        SELECT source_idx, target_idx FROM links 
        WHERE source_idx IN (SELECT idx FROM top_nodes)
          AND target_idx IN (SELECT idx FROM top_nodes)
    """)
    top_links = out_cursor.fetchall()
    print(f"Found {len(top_links)} links between top 100,000 popular nodes.")
    with open(top_links_path, "wb") as f:
        f.write(struct.pack("<I", len(top_links)))
        for src, tgt in top_links:
            f.write(struct.pack("<II", src, tgt))
    
    conn.close()
    out_conn.close()
    print(f"Compilation Complete! Total Nodes: {len(nodes_metadata)}")

if __name__ == "__main__":
    compile_graph()
