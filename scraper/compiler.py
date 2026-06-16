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
    "Biography & People": [
        "births", "deaths", "people", "family", "royalty", "monarch", "king", "queen", "dynasty",
        "biography", "founder", "politician", "peerage", "personalities", "activists", "alumni",
        "educator", "leader", "pioneer", "celebrity", "autobiography", "memoir", "singer", "actor",
        "actress", "musician", "director", "producer", "writer", "author", "philosopher", "theologian",
        "saint", "monk", "president", "prime minister", "governor", "statesman", "philanthropist"
    ],
    "Science & Technology": [
        "science", "physics", "chemistry", "biology", "mathematics", "computer", "programming",
        "software", "technology", "space", "medicine", "engineering", "logic", "network", 
        "academic", "discovery", "invention", "research", "astronomy", "internet", "electronics",
        "device", "algebra", "calculus", "evolution", "psychology", "neuroscience", "robotics",
        "artificial intelligence", "data", "statistic", "physicist", "chemist", "biologist", "scientist"
    ],
    "History & Society": [
        "history", "election", "war", "military", "politics", "empire", "government", "sociology",
        "economy", "society", "battle", "law", "calendar", "treaty", "civilization",
        "revolution", "conflict", "monarchy", "parliament", "treaties", "ancient",
        "medieval", "renaissance", "industrial", "colonial", "democrac", "republic", "party",
        "union", "labor", "rights", "social", "diploma", "policy"
    ],
    "Art & Culture": [
        "art", "music", "film", "television", "entertainment", "sport", "game", "literature",
        "paint", "theater", "show", "media", "album", "drama",
        "culture", "dance", "museum", "fiction", "novel", "poetry", "comedy", "video game",
        "architecture", "sculpture", "design", "fashion", "cuisine", "food", "cook", "creative",
        "performing", "visual", "classic", "contemporary"
    ],
    "Philosophy & Religion": [
        "philosophy", "religion", "myth", "theology", "god", "belief", "deity", "buddhism",
        "christianity", "islam", "hinduism", "judaism", "ethic", "ritual", "spiritual", "church",
        "temple", "bible", "quran", "sacred", "faith", "worship", "existence", "metaphysics", "epistemology"
    ],
    "Geography & Places": [
        "country", "city", "geography", "island", "mountain", "river", "ocean", "sea", "continent",
        "state", "region", "province", "border", "lake", "settlement", "capital", "valley", "coast",
        "town", "village", "landmark", "park", "territory", "district", "county", "location"
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

def classify_topic(title, cleaned_categories, wikidata_type=None):
    """
    Assigns a topic based on Wikidata type (authoritative) 
    with a keyword fallback for legacy data.
    """
    # Authoritative Wikidata Mapping
    if wikidata_type:
        wd = wikidata_type.lower()
        
        # Biography
        if any(k in wd for k in ["human", "person", "man", "woman", "monarch", "politician", "saint", "author", "writer"]):
            return "Biography & People"
        
        # Science & Tech
        if any(k in wd for k in ["science", "technology", "software", "computer", "algorithm", "disease", "medicine", "space", "star", "galaxy"]):
            return "Science & Technology"
            
        # History & Society
        if any(k in wd for k in ["history", "war", "battle", "treaty", "empire", "election", "government", "law", "revolution", "dynasty"]):
            return "History & Society"
            
        # Art & Culture
        if any(k in wd for k in ["film", "movie", "television", "album", "music", "song", "painting", "sculpture", "novel", "book", "sport", "game", "culture"]):
            return "Art & Culture"
            
        # Philosophy & Religion
        if any(k in wd for k in ["philosophy", "religion", "myth", "god", "deity", "spiritual", "church", "temple", "bible"]):
            return "Philosophy & Religion"
            
        # Geography
        if any(k in wd for k in ["country", "city", "island", "mountain", "river", "continent", "state", "region", "settlement"]):
            return "Geography & Places"

        # Websites / Adult to Other
        if any(k in wd for k in ["website", "pornography", "service", "company", "corporation"]):
            return "Other & General"

    # LEGACY FALLBACK: Keyword-based logic
    combined_text = (title + " " + " ".join(cleaned_categories)).lower()
    
    # Priority check for people
    bio_keywords = TOPICS["Biography & People"]
    if any(k in combined_text for k in bio_keywords):
        if not any(k in combined_text for k in ["website", "company", "corporation", "organization", "service"]):
            return "Biography & People"

    # Priority check for websites
    if any(k in combined_text for k in ["website", "pornography", "pornstars", "streaming service", "social network", "dot-com"]):
        return "Other & General"

    scores = {topic: 0 for topic in TOPICS}
    for topic, keywords in TOPICS.items():
        for keyword in keywords:
            if keyword in combined_text:
                scores[topic] += (combined_text.count(keyword) + (5 if keyword in title.lower() else 0))
                
    best_topic = max(scores, key=scores.get)
    return best_topic if scores[best_topic] > 0 else "Other & General"

def compile_graph():
    """Reads database, builds nodes & links, and saves to results/graph_data.json."""
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}. Please run the scraper first.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Select all crawled articles including new Wikidata fields
    cursor.execute("""
        SELECT title, snippet, views, categories, links, wikidata_type 
        FROM articles 
        WHERE crawled = 1 AND snippet IS NOT NULL
    """)
    rows = cursor.fetchall()
    conn.close()
    
    print(f"Loaded {len(rows)} crawled articles from cache.")
    
    # Store article details
    articles_data = {}
    for title, snippet, views, categories, links, wd_type in rows:
        try:
            links_list = json.loads(links) if links else []
        except Exception:
            links_list = []
            
        articles_data[title] = {
            "snippet": snippet,
            "views": views or 0,
            "categories": categories,
            "links": links_list,
            "wd_type": wd_type
        }
        
    valid_titles = set(articles_data.keys())
    print(f"Found {len(valid_titles)} valid nodes in the crawled set.")
    
    # Keep ALL valid links between crawled nodes for the pathfinder
    links_json = []
    seen_links = set()
    
    for source, info in articles_data.items():
        for target in info["links"]:
            if target in valid_titles:
                link_key = tuple(sorted([source, target]))
                if link_key not in seen_links:
                    seen_links.add(link_key)
                    links_json.append({"source": source, "target": target})
    
    in_degrees = {title: 0 for title in valid_titles}
    out_degrees = {title: 0 for title in valid_titles}
    for l in links_json:
        out_degrees[l["source"]] += 1
        in_degrees[l["target"]] += 1
                    
    # Generate nodes list
    nodes_json = []
    for title, info in articles_data.items():
        cleaned_cats = clean_categories(info["categories"])
        category = classify_topic(title, cleaned_cats, info["wd_type"])
        
        nodes_json.append({
            "id": title,
            "views": info["views"],
            "snippet": info["snippet"],
            "category": category,
            "inDegree": in_degrees[title],
            "outDegree": out_degrees[title],
            "wd_type": info["wd_type"]
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
    Initializes coordinates to 0,0 so that layout positions are computed
    on the frontend and saved back to the database.
    """
    for n in nodes:
        n["x"] = 0.0
        n["y"] = 0.0

if __name__ == "__main__":
    compile_graph()
