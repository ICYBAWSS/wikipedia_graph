import os
import json
import sqlite3

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "wiki_cache.db"))
OUTPUT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "results", "graph_data.json"))

# Blacklist prefixes/keywords for administrative categories
CAT_BLACKLIST_PREFIXES = (
    "Category:All ", "Category:Articles ", "Category:Use ", "Category:Short description",
    "Category:Pages ", "Category:Wikipedia ", "Category:CS1 ", "Category:Webarchive ",
    "Category:Template ", "Category:Good articles", "Category:Featured articles",
    "Category:Spoken articles", "Category:Link ", "Category:Tracked ", "Category:Identifiers ",
    "Category:ACAD ", "Category:VIAF ", "Category:LCCN ", "Category:GND ", "Category:Sudoc ",
    "Category:BNF "
)

# Categorization rules based on keywords found in categories
TOPICS = {
    "Science & Technology": [
        "science", "physics", "chemistry", "biology", "mathematics", "computer", "programming",
        "software", "technology", "space", "medicine", "engineering", "logic", "network", 
        "academic", "discovery", "invention", "research", "astronomy", "internet", "electronics",
        "device", "algebra", "calculus", "evolution", "psychology", "neuroscience", "robotics",
        "artificial intelligence", "data", "statistic", "physicist", "chemist", "biologist", "scientist"
    ],
    "History & Society": [
        "history", "election", "war", "military", "politics", "empire", "government", "sociology",
        "economy", "society", "battle", "law", "dynasty", "calendar", "treaty", "civilization",
        "revolution", "conflict", "monarchy", "president", "parliament", "treaties", "ancient",
        "medieval", "renaissance", "industrial", "colonial", "democrac", "republic", "party",
        "union", "labor", "rights", "social", "diploma", "policy"
    ],
    "Art & Culture": [
        "art", "music", "film", "television", "entertainment", "sport", "game", "literature",
        "writer", "paint", "theater", "show", "media", "album", "singer", "actor", "drama",
        "culture", "dance", "museum", "fiction", "novel", "poetry", "comedy", "video game",
        "architecture", "sculpture", "design", "fashion", "cuisine", "food", "cook", "creative",
        "performing", "visual", "classic", "contemporary", "musician", "director", "producer"
    ],
    "Philosophy & Religion": [
        "philosophy", "religion", "myth", "theology", "god", "belief", "deity", "buddhism",
        "christianity", "islam", "hinduism", "judaism", "ethic", "ritual", "spiritual", "church",
        "temple", "bible", "quran", "sacred", "philosopher", "theologian", "monk", "saint",
        "faith", "worship", "existence", "metaphysics", "epistemology"
    ],
    "Geography & Places": [
        "country", "city", "geography", "island", "mountain", "river", "ocean", "sea", "continent",
        "state", "region", "province", "border", "lake", "settlement", "capital", "valley", "coast",
        "town", "village", "landmark", "park", "territory", "district", "county", "location"
    ],
    "Biography & People": [
        "births", "deaths", "people", "family", "royalty", "monarch", "king", "queen", "dynasty",
        "biography", "founder", "politician", "peerage", "personalities", "activists", "alumni",
        "educator", "leader", "pioneer", "celebrity", "autobiography", "memoir"
    ]
}

def clean_categories(raw_categories_json):
    """Filters out administrative Wikipedia categories."""
    if not raw_categories_json:
        return []
    try:
        cats = json.loads(raw_categories_json)
        cleaned = []
        for cat in cats:
            if not any(cat.startswith(p) for p in CAT_BLACKLIST_PREFIXES):
                cleaned.append(cat.lower())
        return cleaned
    except Exception:
        return []

def classify_topic(title, cleaned_categories):
    """
    Classifies an article into a top-level topic based on categories
    and title keywords.
    """
    scores = {topic: 0 for topic in TOPICS}
    text_to_scan = " ".join(cleaned_categories) + " " + title.lower()
    
    for topic, keywords in TOPICS.items():
        for keyword in keywords:
            # Check for word match to avoid substring false positives (e.g. 'art' inside 'earth')
            if keyword in text_to_scan:
                scores[topic] += text_to_scan.count(keyword)
                
    # Find the topic with the highest score
    best_topic = "Other & General"
    best_score = 0
    for topic, score in scores.items():
        if score > best_score:
            best_score = score
            best_topic = topic
            
    return best_topic

def compile_graph():
    """Reads database, builds nodes & links, and saves to results/graph_data.json."""
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}. Please run the scraper first.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Select all crawled articles
    cursor.execute("""
        SELECT title, snippet, views, categories, links 
        FROM articles 
        WHERE crawled = 1 AND snippet IS NOT NULL
    """)
    rows = cursor.fetchall()
    conn.close()
    
    print(f"Loaded {len(rows)} crawled articles from cache.")
    
    # Store article details
    articles_data = {}
    for title, snippet, views, categories, links in rows:
        try:
            links_list = json.loads(links) if links else []
        except Exception:
            links_list = []
            
        articles_data[title] = {
            "snippet": snippet,
            "views": views or 0,
            "categories": categories,
            "links": links_list
        }
        
    valid_titles = set(articles_data.keys())
    print(f"Found {len(valid_titles)} valid nodes in the crawled set.")
    
    # Prune links to limit density while keeping the most popular outbound connections
    max_outbound_links = 30  # Increased from 6 to improve pathfinder connectivity
    for source, info in articles_data.items():
        valid_outbound = [t for t in info["links"] if t in valid_titles]
        # Sort targets by views (popularity) descending
        valid_outbound_sorted = sorted(
            valid_outbound,
            key=lambda t: articles_data[t]["views"],
            reverse=True
        )
        # Keep only the top K
        info["links"] = valid_outbound_sorted[:max_outbound_links]
    
    # Compute in-degree and out-degree
    in_degrees = {title: 0 for title in valid_titles}
    out_degrees = {title: 0 for title in valid_titles}
    
    links_json = []
    seen_links = set()
    
    for source, info in articles_data.items():
        for target in info["links"]:
            if target in valid_titles:
                out_degrees[source] += 1
                in_degrees[target] += 1
                
                # Check for duplicate undirected link representation
                link_key = tuple(sorted([source, target]))
                if link_key not in seen_links:
                    seen_links.add(link_key)
                    links_json.append({
                        "source": source,
                        "target": target
                    })
                    
    # Generate nodes list
    nodes_json = []
    for title, info in articles_data.items():
        # Clean and classify
        cleaned_cats = clean_categories(info["categories"])
        category = classify_topic(title, cleaned_cats)
        
        nodes_json.append({
            "id": title,
            "views": info["views"],
            "snippet": info["snippet"],
            "category": category,
            "inDegree": in_degrees[title],
            "outDegree": out_degrees[title]
        })
        
    # Calculate coordinate layout positions offline
    print("Running coordinate layout positioning algorithm...")
    run_spring_layout(nodes_json, links_json)
    
    # Save compiled network to SQLite database
    db_out_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "results", "wiki_graph.db"))
    print(f"Writing compiled database to {db_out_path}...")
    if os.path.exists(db_out_path):
        os.remove(db_out_path)
        
    os.makedirs(os.path.dirname(db_out_path), exist_ok=True)
    conn = sqlite3.connect(db_out_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            category TEXT,
            views INTEGER,
            inDegree INTEGER,
            outDegree INTEGER,
            x REAL,
            y REAL,
            snippet TEXT
        )
    """)
    
    cursor.execute("""
        CREATE TABLE links (
            source TEXT,
            target TEXT
        )
    """)
    
    # Insert node details
    cursor.executemany("""
        INSERT INTO nodes (id, category, views, inDegree, outDegree, x, y, snippet)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (n["id"], n["category"], n["views"], n["inDegree"], n["outDegree"], n["x"], n["y"], n["snippet"])
        for n in nodes_json
    ])
    
    # Insert link connections
    cursor.executemany("""
        INSERT INTO links (source, target)
        VALUES (?, ?)
    """, [
        (l["source"], l["target"])
        for l in links_json
    ])
    
    # Create indexes for fast querying in WASM
    cursor.execute("CREATE INDEX idx_nodes_xy ON nodes (x, y)")
    cursor.execute("CREATE INDEX idx_nodes_views ON nodes (views)")
    cursor.execute("CREATE INDEX idx_nodes_indegree ON nodes (inDegree)")
    cursor.execute("CREATE INDEX idx_links_src ON links (source)")
    cursor.execute("CREATE INDEX idx_links_tgt ON links (target)")
    
    conn.commit()
    conn.close()
    
    print("Compiled database successfully!")
    print(f"Total Nodes: {len(nodes_json)}")
    print(f"Total Links: {len(links_json)}")

def run_spring_layout(nodes, links, iterations=80, k=None):
    """
    Dependency-free Fruchterman-Reingold layout algorithm / Semantic spiral hybrid.
    Computes static x and y coordinates for each node offline.
    """
    import math
    import random
    
    n_count = len(nodes)
    if n_count == 0:
        return
        
    pos = {}
    node_ids = [n["id"] for n in nodes]
    
    # If graph is large (>= 3000 nodes), use the fast hierarchical category spiral layout
    if n_count >= 3000:
        cat_groups = {}
        for n in nodes:
            cat = n["category"]
            cat_groups.setdefault(cat, []).append(n)
            
        categories = list(cat_groups.keys())
        num_cats = len(categories)
        
        cat_centers = {}
        for idx, cat in enumerate(categories):
            angle = (idx / num_cats) * 2.0 * math.pi
            r_center = 1200.0
            cat_centers[cat] = [r_center * math.cos(angle), r_center * math.sin(angle)]
            
        for cat, cat_nodes in cat_groups.items():
            cx, cy = cat_centers[cat]
            cat_nodes_sorted = sorted(cat_nodes, key=lambda n: n["inDegree"], reverse=True)
            for idx, n in enumerate(cat_nodes_sorted):
                theta = idx * 0.15
                r_node = 35.0 * math.sqrt(idx) + 50.0
                pos[n["id"]] = [
                    cx + r_node * math.cos(theta),
                    cy + r_node * math.sin(theta)
                ]
    else:
        # Fruchterman-Reingold spring simulation
        # Initialize positions randomly in center area
        for nid in node_ids:
            pos[nid] = [random.uniform(-400, 400), random.uniform(-400, 400)]
            
        if k is None:
            k = math.sqrt(3000000.0 / n_count)
            
        temp = 100.0
        dt = temp / iterations
        
        for step in range(iterations):
            disp = {nid: [0.0, 0.0] for nid in node_ids}
            
            # Repulsive forces
            for i in range(n_count):
                nid_i = node_ids[i]
                px_i, py_i = pos[nid_i]
                for j in range(i + 1, n_count):
                    nid_j = node_ids[j]
                    px_j, py_j = pos[nid_j]
                    
                    dx = px_i - px_j
                    dy = py_i - py_j
                    dist2 = dx * dx + dy * dy
                    dist = math.sqrt(dist2)
                    if dist == 0:
                        dist = 0.1
                        dx = 0.1
                        
                    fr = (k * k) / dist
                    fx = (dx / dist) * fr
                    fy = (dy / dist) * fr
                    
                    disp[nid_i][0] += fx
                    disp[nid_i][1] += fy
                    disp[nid_j][0] -= fx
                    disp[nid_j][1] -= fy
                    
            # Attractive forces along links
            for l in links:
                s, t = l["source"], l["target"]
                if s not in pos or t not in pos:
                    continue
                px_s, py_s = pos[s]
                px_t, py_t = pos[t]
                
                dx = px_s - px_t
                dy = py_s - py_t
                dist = math.sqrt(dx * dx + dy * dy)
                if dist == 0:
                    dist = 0.1
                    dx = 0.1
                    
                fa = (dist * dist) / k
                fx = (dx / dist) * fa
                fy = (dy / dist) * fa
                
                disp[s][0] -= fx
                disp[s][1] -= fy
                disp[t][0] += fx
                disp[t][1] += fy
                
            # Displace nodes constrained by temperature
            for nid in node_ids:
                dx, dy = disp[nid]
                dist = math.sqrt(dx * dx + dy * dy)
                if dist == 0:
                    continue
                scale = min(dist, temp) / dist
                pos[nid][0] += dx * scale
                pos[nid][1] += dy * scale
                
            temp -= dt
            
    # Save positions
    for n in nodes:
        n["x"] = pos[n["id"]][0]
        n["y"] = pos[n["id"]][1]

if __name__ == "__main__":
    compile_graph()
