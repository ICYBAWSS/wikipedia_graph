import sys
import os
import sqlite3
import struct
import numpy as np
import json

# Add graph-tool to path
gt_site_packages = "/opt/homebrew/opt/graph-tool/libexec/lib/python3.14/site-packages"
if gt_site_packages not in sys.path:
    sys.path.append(gt_site_packages)

try:
    from graph_tool.all import Graph, graph_draw
except ImportError:
    print(f"Error: graph-tool not found in {gt_site_packages}")
    sys.exit(1)


def get_next_output_path(base_name="massive_galaxy_static_asis"):
    i = 1
    while True:
        candidate = f"{base_name}{i}.png"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def get_category_colors(num_nodes, struct_db="wiki_graph_structure.db", cache_db="test_scrape/wiki_cache.db"):
    """Fuzzy-map millions of sub-categories into 9 vibrant parent categories."""
    print("Step 4: Mapping nodes to 9 semantic categories...")
    
    categories = {
        "History & Society": {"color": [0.9, 0.1, 0.1, 0.9], "keywords": ["history", "war", "politics", "births", "deaths", "people", "century", "assassinated"]},
        "Science & Math": {"color": [0.1, 0.5, 0.9, 0.9], "keywords": ["physics", "biology", "chemistry", "math", "astronomy", "climatology", "radiation"]},
        "Arts & Entertainment": {"color": [0.8, 0.2, 0.8, 0.9], "keywords": ["music", "film", "art", "theatre", "album", "actor", "academy awards", "painting"]},
        "Geography & Nature": {"color": [0.1, 0.8, 0.2, 0.9], "keywords": ["geography", "country", "city", "island", "mountain", "states", "river", "ocean"]},
        "Technology": {"color": [0.1, 0.9, 0.9, 0.9], "keywords": ["technology", "computer", "software", "internet", "engineering", "machine"]},
        "Culture & Philosophy": {"color": [0.9, 0.6, 0.1, 0.9], "keywords": ["culture", "religion", "philosophy", "mythology", "mythological", "deity"]},
        "Health & Wellness": {"color": [0.9, 0.3, 0.5, 0.9], "keywords": ["medicine", "health", "disease", "anatomy", "medical"]},
        "Sports": {"color": [0.8, 0.8, 0.1, 0.9], "keywords": ["sports", "football", "baseball", "basketball", "olympics", "racing"]},
        "Business": {"color": [0.5, 0.8, 0.9, 0.9], "keywords": ["business", "economy", "company", "finance", "industry", "market"]}
    }
    
    colors_array = np.full((4, num_nodes), 0.3, dtype=np.float64)
    colors_array[3, :] = 0.2 # low alpha for default dim grey
    
    try:
        conn_struct = sqlite3.connect(struct_db)
        title_to_id = {row[1]: row[0] for row in conn_struct.execute("SELECT id, title FROM nodes")}
        conn_struct.close()
        
        conn_cache = sqlite3.connect(cache_db)
        print("  Scanning categories and applying fuzzy matches...")
        for title, cat_json in conn_cache.execute("SELECT title, categories FROM articles"):
            if title in title_to_id:
                idx = title_to_id[title]
                # Ensure we don't out-of-bounds map if num_nodes is smaller than total DB items
                if idx >= num_nodes:
                    continue
                if cat_json:
                    try:
                        cats_list = [c.lower() for c in json.loads(cat_json)]
                        found = False
                        for cat_name, info in categories.items():
                            for kw in info["keywords"]:
                                if any(kw in c for c in cats_list):
                                    colors_array[:, idx] = info["color"]
                                    found = True
                                    break
                            if found: break
                    except: pass
        conn_cache.close()
    except Exception as e:
        print(f"  Note: Mapping failed ({e}), using default colors.")
        
    return colors_array


def render_massive_galaxy():
    structure_db = "wiki_graph_structure.db"
    cache_db = "test_scrape/wiki_cache.db"
    bin_path = "coordinates.bin"
    output_image = get_next_output_path("massive_galaxy_static_asis")

    if not os.path.exists(bin_path):
        print("Error: coordinates.bin not found.")
        return

    # --- 1. READ BINARY COORDINATES FIRST ---
    print("Step 1: Loading Raw Enriched Coordinates...")
    with open(bin_path, "rb") as f:
        file_bytes = f.read()
        data_bytes = file_bytes[4:] # Skip header
        total_floats = len(data_bytes) // 4
        
        print(f"  Detected {total_floats:,} total float elements in file.")
        
        if total_floats % 5 == 0:
            cols = 5
        elif total_floats % 4 == 0:
            print("  [Warning]: Data is divisible by 4, not 5. Adjusting layout assumption.")
            cols = 4
        else:
            cols = 5
            total_floats -= (total_floats % 5)

        raw_floats = np.frombuffer(data_bytes, dtype=np.float32)[:total_floats]
        data = raw_floats.reshape(-1, cols)
        coords = data[:, 0:2].copy()
        
    num_coords = coords.shape[0]
    print(f"  Successfully loaded coordinates matrix of shape: {coords.shape}")

    # --- 2. INITIALIZE GRAPH STRUCTURE ---
    print(f"Step 2: Initializing Graph Structure with {num_coords:,} nodes...")
    g = Graph(directed=False)
    g.add_vertex(num_coords)
    
    # --- 3. LOAD FILAMENTS (EDGES) ---
    edge_sample = 5000000 
    print(f"Step 3: Loading up to {edge_sample:,} edges for filaments...")
    conn = sqlite3.connect(structure_db)
    cursor = conn.cursor()
    
    # Filter links to ensure indices match the loaded coordinates space
    cursor.execute("""
        SELECT source_idx, target_idx FROM links 
        WHERE source_idx < ? AND target_idx < ? 
        LIMIT ?
    """, (num_coords, num_coords, edge_sample))
    
    valid_edges = cursor.fetchall()
    g.add_edge_list(valid_edges)
    conn.close()
    print(f"  Loaded {len(valid_edges):,} valid links.")

    # --- 4. ASSIGN GRAPH PROPERTIES ---
    pos = g.new_vertex_property("vector<double>")
    pos.set_2d_array(coords.T)
    
    colors_matrix = get_category_colors(num_coords, structure_db, cache_db)
    v_color = g.new_vertex_property("vector<double>")
    v_color.set_2d_array(colors_matrix)

    # --- 5. RENDER THE PLOT ---
    print(f"Step 5: Rendering high-res RAW image to {output_image}...")
    print("  This is a massive CPU task. Monitor RAM usage.")
    
    graph_draw(
        g,
        pos=pos,
        output=output_image,
        output_size=(18000, 18000), 
        vertex_size=1.5, 
        vertex_fill_color=v_color,
        vertex_pen_width=0, 
        edge_pen_width=0.08, 
        edge_color=[1, 1, 1, 0.08], 
        bg_color=[0, 0, 0, 1],
        preview=False
    )
    
    print(f"Done! Open {output_image} to see the raw, unprocessed galaxy.")


if __name__ == "__main__":
    render_massive_galaxy()
