import sqlite3
import math
import struct
import time
import os
import random

DB_PATH = "wiki_simulation.db"
BIN_PATH = "coordinates.bin"
NUM_NODES = 8_000_000
BATCH_SIZE = 200_000

def main():
    # Set random seed for reproducibility
    random.seed(42)
    
    phi = (1 + 5**0.5) / 2
    num_categories = 7
    num_sub_clusters = 12
    
    print("Step 1: Re-generating coordinates.bin with Galactic Hub-and-Spoke layout...")
    start_time = time.time()
    
    with open(BIN_PATH, "wb") as f:
        f.write(struct.pack("I", NUM_NODES))
        for j in range(NUM_NODES):
            # 1. Category Galaxy Center
            cat_idx = j % num_categories
            cat_angle = 2 * math.pi * cat_idx / num_categories
            # Large radius for the 7 category centers
            cx = 850.0 * math.cos(cat_angle)
            cy = 850.0 * math.sin(cat_angle)
            
            # 2. Sub-cluster Center within the Galaxy
            sub_idx = (j // num_categories) % num_sub_clusters
            sub_angle = 2 * math.pi * sub_idx / num_sub_clusters
            # Radius for the sub-clusters around the galaxy center
            sub_cx = cx + 220.0 * math.cos(sub_angle)
            sub_cy = cy + 220.0 * math.sin(sub_angle)
            
            # 3. Local Sunflower Spiral coordinate within the sub-cluster
            k = (j // num_categories) // num_sub_clusters
            local_angle = 2 * math.pi * phi * k
            # Local radius - expands up to ~120
            local_r = 0.39 * (k**0.5)
            
            # 4. Organic Jitter/Noise to break the rigid coordinate grid
            # Jitter increases slightly as we go outward, creating fuzzy cluster edges
            jitter_max = 1.8 + 0.12 * local_r
            jitter_angle = random.random() * 2 * math.pi
            jitter_r = random.random() * jitter_max
            
            lx = (local_r + jitter_r * math.cos(jitter_angle)) * math.cos(local_angle)
            ly = (local_r + jitter_r * math.sin(jitter_angle)) * math.sin(local_angle)
            
            # Final position
            x = sub_cx + lx
            y = sub_cy + ly
            
            f.write(struct.pack("ff", x, y))
            
    print(f"coordinates.bin written successfully in {time.time() - start_time:.2f}s.")
    
    if not os.path.exists(DB_PATH):
        print(f"Database {DB_PATH} not found. Skipping DB update.")
        return
        
    print(f"Step 2: Updating {DB_PATH} coordinates...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check if index exists and drop it to speed up updates
    print("Checking for idx_nodes_xy...")
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_nodes_xy'")
    idx_exists = cursor.fetchone()
    if idx_exists:
        print("Dropping index idx_nodes_xy...")
        cursor.execute("DROP INDEX idx_nodes_xy")
        conn.commit()
        
    print("Updating nodes table in batches...")
    start_time = time.time()
    
    # Reset random seed for database updates to match coordinates.bin exactly
    random.seed(42)
    
    for i in range(0, NUM_NODES, BATCH_SIZE):
        batch = []
        end = min(i + BATCH_SIZE, NUM_NODES)
        for j in range(i, end):
            cat_idx = j % num_categories
            cat_angle = 2 * math.pi * cat_idx / num_categories
            cx = 850.0 * math.cos(cat_angle)
            cy = 850.0 * math.sin(cat_angle)
            
            sub_idx = (j // num_categories) % num_sub_clusters
            sub_angle = 2 * math.pi * sub_idx / num_sub_clusters
            sub_cx = cx + 220.0 * math.cos(sub_angle)
            sub_cy = cy + 220.0 * math.sin(sub_angle)
            
            k = (j // num_categories) // num_sub_clusters
            local_angle = 2 * math.pi * phi * k
            local_r = 0.39 * (k**0.5)
            
            jitter_max = 1.8 + 0.12 * local_r
            jitter_angle = random.random() * 2 * math.pi
            jitter_r = random.random() * jitter_max
            
            lx = (local_r + jitter_r * math.cos(jitter_angle)) * math.cos(local_angle)
            ly = (local_r + jitter_r * math.sin(jitter_angle)) * math.sin(local_angle)
            
            x = sub_cx + lx
            y = sub_cy + ly
            
            rowid = j + 1
            batch.append((x, y, rowid))
            
        cursor.executemany("UPDATE nodes SET x = ?, y = ? WHERE rowid = ?", batch)
        conn.commit()
        print(f"  Updated nodes {i} to {end}...")
        
    print(f"Database coordinates updated in {time.time() - start_time:.2f}s.")
    
    print("Re-creating index idx_nodes_xy...")
    start_time = time.time()
    cursor.execute("CREATE INDEX idx_nodes_xy ON nodes (x, y)")
    conn.commit()
    print(f"Index recreated in {time.time() - start_time:.2f}s.")
    
    # Verify a few nodes
    cursor.execute("SELECT id, x, y FROM nodes LIMIT 5")
    print("Verifying first 5 nodes:")
    for row in cursor.fetchall():
        print(f"  {row[0]}: x={row[1]}, y={row[2]}")
        
    conn.close()
    print("All coordinates updated and verified successfully!")

if __name__ == "__main__":
    main()
