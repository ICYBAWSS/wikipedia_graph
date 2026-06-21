import sqlite3
import random
import os
import time

DB_PATH = "wiki_simulation.db"
NUM_NODES = 8_000_000
AVG_LINKS_PER_NODE = 25 # Wikipedia average
BATCH_SIZE = 100_000

TOPICS = [
    "Biography & People", "Science & Technology", "Arts & Culture", 
    "History & Events", "Geography & Places", "Philosophy & Religion", 
    "Politics & Government", "Sports & Games", "Other & General"
]

def generate_simulation():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Create Tables
    print("Creating tables...")
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
            target TEXT,
            context TEXT
        )
    """)
    conn.commit()

    # 2. Generate Nodes
    print(f"Generating {NUM_NODES} nodes...")
    start_time = time.time()
    for i in range(0, NUM_NODES, BATCH_SIZE):
        batch = []
        end = min(i + BATCH_SIZE, NUM_NODES)
        for j in range(i, end):
            node_id = f"Node_{j+1:07d}"
            # Random views, lower IDs are "older" and more popular on average
            views = int(random.expovariate(1/100000)) + random.randint(0, 1000)
            category = random.choice(TOPICS)
            # Correct Spiral (Sunflower) layout for simulation
            import math
            # Golden angle in radians
            phi = (1 + 5**0.5) / 2
            angle = 2 * math.pi * phi * j
            r = 5.0 * j**0.5
            x = r * math.cos(angle)
            y = r * math.sin(angle)
            snippet = f"This is a simulated Wikipedia article for {node_id}, categorized under {category}."
            
            batch.append((node_id, category, views, 0, 0, x, y, snippet))
        
        cursor.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?)", batch)
        if (i + BATCH_SIZE) % 1_000_000 == 0:
            print(f"  Inserted {i + BATCH_SIZE} nodes...")
            conn.commit()
    
    print(f"Nodes generated in {time.time() - start_time:.2f}s.")

    # 3. Generate Links (Power Law)
    print(f"Generating ~{NUM_NODES * AVG_LINKS_PER_NODE} links...")
    start_time = time.time()
    
    total_links = 0
    # Store degrees in memory for small N, or use a separate table for large N
    out_degrees = {}
    
    for i in range(0, NUM_NODES, BATCH_SIZE):
        batch_links = []
        end = min(i + BATCH_SIZE, NUM_NODES)
        for j in range(i, end):
            source_id = f"Node_{j+1:07d}"
            num_links = random.randint(5, AVG_LINKS_PER_NODE * 2)
            out_degrees[source_id] = num_links
            
            for _ in range(num_links):
                # Power law target selection
                target_idx = int((random.random()**3.0) * NUM_NODES) + 1
                if target_idx > NUM_NODES: target_idx = NUM_NODES
                target_id = f"Node_{target_idx:07d}"
                
                if source_id != target_id:
                    ctx = f"In the article {source_id}, we find a significant connection to {target_id}."
                    batch_links.append((source_id, target_id, ctx))
                    total_links += 1
        
        cursor.executemany("INSERT INTO links VALUES (?,?,?)", batch_links)
        if (i + BATCH_SIZE) % 500_000 == 0:
            print(f"  Inserted {total_links} links...")
            conn.commit()

    print(f"Links generated in {time.time() - start_time:.2f}s.")

    # 4. Update Degrees (Optimized)
    print("Updating degrees via temporary mapping...")
    start_time = time.time()
    # For simulation, we'll just update outDegree using our memory map
    # To handle 8M nodes without RAM issues, we process in chunks
    node_ids = list(out_degrees.keys())
    for i in range(0, len(node_ids), BATCH_SIZE):
        chunk = node_ids[i:i+BATCH_SIZE]
        updates = [(out_degrees[nid], nid) for nid in chunk]
        cursor.executemany("UPDATE nodes SET outDegree = ? WHERE id = ?", updates)
    conn.commit()
    print(f"Degrees updated in {time.time() - start_time:.2f}s.")

    # 5. Build Indices and FTS5 for Search
    print("Building indices and FTS5 search table...")
    start_time = time.time()
    cursor.execute("CREATE INDEX idx_nodes_xy ON nodes (x, y)")
    cursor.execute("CREATE INDEX idx_nodes_views ON nodes (views)")
    cursor.execute("CREATE INDEX idx_links_src ON links (source)")
    cursor.execute("CREATE INDEX idx_links_tgt ON links (target)")
    
    # Create Full-Text Search table for instant searching of 8M titles
    cursor.execute("CREATE VIRTUAL TABLE nodes_fts USING fts5(id, category, content='nodes', content_rowid='rowid')")
    cursor.execute("INSERT INTO nodes_fts(rowid, id, category) SELECT rowid, id, category FROM nodes")
    
    conn.commit()
    print(f"Indices and FTS5 built in {time.time() - start_time:.2f}s.")

    # 6. Export Binary Coordinates for WebGL
    print("Exporting binary coordinates for WebGL engine...")
    import struct
    bin_path = "coordinates.bin"
    cursor.execute("SELECT x, y FROM nodes ORDER BY id") # Order must be consistent
    with open(bin_path, "wb") as f:
        # Write total count first
        f.write(struct.pack("I", NUM_NODES))
        # Write all x,y as float32
        for x, y in cursor:
            f.write(struct.pack("ff", x, y))
    
    print(f"Binary coordinates exported to {bin_path}")

    conn.close()
    print(f"Simulation complete! Database saved to {DB_PATH}")
    file_size = os.path.getsize(DB_PATH) / (1024 * 1024 * 1024)
    print(f"Final DB Size: {file_size:.2f} GB")

if __name__ == "__main__":
    generate_simulation()
