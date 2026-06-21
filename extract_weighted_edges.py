import sqlite3
import json
import csv
import time
import os
import gzip

# This script extracts nodes AND edges with weights based on link order.
# Rationale: The first links in a Wikipedia article are usually broad categories (high importance).

def extract_weighted_data():
    cache_db = "test_scrape/wiki_cache.db"
    struct_db = "wiki_graph_structure.db"
    out_meta = "metadata.csv"
    out_edges = "edges_weighted.csv.gz"

    if not os.path.exists(cache_db) or not os.path.exists(struct_db):
        print("Error: Required databases not found.")
        return

    print("--- ICYBAWSS Weighted Extractor ---")
    
    # 1. Map Titles to IDs from the structure DB
    print("Step 1: Mapping titles to IDs...")
    conn_struct = sqlite3.connect(struct_db)
    title_to_id = {row[1]: row[0] for row in conn_struct.execute("SELECT id, title FROM nodes")}
    num_nodes = len(title_to_id)
    conn_struct.close()

    # 2. Extract Metadata & Weighted Edges in one pass
    print("Step 2: Processing 21GB cache for categories and weighted links...")
    conn_cache = sqlite3.connect(cache_db)
    cursor = conn_cache.cursor()
    cursor.execute("SELECT title, views, categories, links FROM articles")

    # Metadata storage
    meta_rows = [None] * num_nodes
    
    # Edge storage (Gzipped CSV)
    start_time = time.time()
    edges_count = 0
    
    # Classification Logic (same as before)
    TOPICS = {
        "Biography & People": ["births", "deaths", "people", "family", "royalty", "monarch", "king", "queen", "dynasty", "biography", "founder", "politician", "peerage", "personalities", "activists", "alumni", "educator", "leader", "pioneer", "celebrity", "autobiography", "memoir", "singer", "actor", "actress", "musician", "director", "producer", "writer", "author", "philosopher", "theologian", "saint", "monk", "president", "prime minister", "governor", "statesman", "philanthropist"],
        "Science & Technology": ["science", "physics", "chemistry", "biology", "mathematics", "computer", "programming", "software", "technology", "space", "medicine", "engineering", "logic", "network", "academic", "discovery", "invention", "research", "astronomy", "internet", "electronics", "device", "algebra", "calculus", "evolution", "psychology", "neuroscience", "robotics", "artificial intelligence", "data", "statistic", "physicist", "chemist", "biologist", "scientist"],
        "History & Society": ["history", "election", "war", "military", "politics", "empire", "government", "sociology", "economy", "society", "battle", "law", "calendar", "treaty", "civilization", "revolution", "conflict", "monarchy", "parliament", "treaties", "ancient", "medieval", "renaissance", "industrial", "colonial", "democrac", "republic", "party", "union", "labor", "rights", "social", "diploma", "policy"],
        "Art & Culture": ["art", "music", "film", "television", "entertainment", "sport", "game", "literature", "paint", "theater", "show", "media", "album", "drama", "culture", "dance", "museum", "fiction", "novel", "poetry", "comedy", "video game", "architecture", "sculpture", "design", "fashion", "cuisine", "food", "cook", "creative", "performing", "visual", "classic", "contemporary"],
        "Philosophy & Religion": ["philosophy", "religion", "myth", "theology", "god", "belief", "deity", "buddhism", "christianity", "islam", "hinduism", "judaism", "ethic", "ritual", "spiritual", "church", "temple", "bible", "quran", "sacred", "faith", "worship", "existence", "metaphysics", "epistemology"],
        "Geography & Places": ["country", "city", "geography", "island", "mountain", "river", "ocean", "sea", "continent", "state", "region", "province", "border", "lake", "settlement", "capital", "valley", "coast", "town", "village", "landmark", "park", "territory", "district", "county", "location"]
    }
    cat_to_id = {name: i for i, name in enumerate(TOPICS.keys())}
    
    def classify(title, cats_json):
        if not cats_json: return 6
        try: cats = [c.lower() for c in json.loads(cats_json)]
        except: return 6
        combined = (str(title) + " " + " ".join(cats)).lower()
        scores = {topic: 0 for topic in TOPICS}
        for topic, keywords in TOPICS.items():
            for kw in keywords:
                if kw in combined: scores[topic] += (combined.count(kw) + (5 if kw in str(title).lower() else 0))
        best = max(scores, key=scores.get)
        return cat_to_id[best] if scores[best] > 0 else 6

    with gzip.open(out_edges, "wt", encoding="utf-8") as f_edges:
        f_edges.write("source,target,weight\n")
        
        processed = 0
        for title, views, cats, links_json in cursor:
            if title not in title_to_id: continue
            
            src_idx = title_to_id[title]
            
            # Save metadata
            meta_rows[src_idx] = [src_idx, views or 0, classify(title, cats)]
            
            # Extract Weighted Links
            if links_json:
                try:
                    targets = json.loads(links_json)
                    for i, target_title in enumerate(targets):
                        if target_title in title_to_id:
                            tgt_idx = title_to_id[target_title]
                            # Weighting formula: 1.0 for first link, decaying to 0.1
                            # This makes "Lead Links" acts as the primary anchors.
                            weight = max(0.1, 1.0 / (1.0 + i * 0.05))
                            f_edges.write(f"{src_idx},{tgt_idx},{weight:.3f}\n")
                            edges_count += 1
                except: pass

            processed += 1
            if processed % 500000 == 0:
                elapsed = time.time() - start_time
                print(f"  Processed {processed:,} nodes... {edges_count:,} edges found. ({elapsed:.1f}s)")

    conn_cache.close()

    # 3. Save Metadata
    print("Step 3: Saving metadata.csv...")
    with open(out_meta, "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["id", "views", "category_id"])
        # Fill any missing indices with defaults
        for i in range(num_nodes):
            if meta_rows[i] is None: writer.writerow([i, 0, 6])
            else: writer.writerow(meta_rows[i])

    print(f"\nDone! Extracted {num_nodes:,} nodes and {edges_count:,} weighted edges.")
    print(f"Files ready: {out_meta}, {out_edges}")

if __name__ == "__main__":
    extract_weighted_data()
