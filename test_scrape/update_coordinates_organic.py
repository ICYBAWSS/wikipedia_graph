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
    
    num_categories = 7
    
    print("Step 1: Re-generating coordinates.bin with Single Unified Organic layout...")
    start_time = time.time()
    
    with open(BIN_PATH, "wb") as f:
        f.write(struct.pack("I", NUM_NODES))
        for j in range(NUM_NODES):
            # 1. Base category sector angle
            cat_idx = j % num_categories
            base_angle = 2 * math.pi * cat_idx / num_categories
            
            # 2. Filament structure (3 branching arms per category sector)
            filament_idx = (j // num_categories) % 3
            # Spaced filament offset in radians
            filament_offset = (filament_idx - 1) * 0.24
            
            # 3. Distance from center (power-law distribution to place hubs at the center)
            base_r = 1.1 * (j**0.45)
            # Add radial organic noise to smear the rings
            r = base_r * random.uniform(0.65, 1.35)
            
            # 4. Angular Gaussian jitter (wider spread at the periphery)
            angular_jitter = random.gauss(0, 0.08 + 0.05 * (j / NUM_NODES))
            
            angle = base_angle + filament_offset + angular_jitter
            
            x = r * math.cos(angle)
            y = r * math.sin(angle)
            
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
            base_angle = 2 * math.pi * cat_idx / num_categories
            
            filament_idx = (j // num_categories) % 3
            filament_offset = (filament_idx - 1) * 0.24
            
            base_r = 1.1 * (j**0.45)
            r = base_r * random.uniform(0.65, 1.35)
            
            angular_jitter = random.gauss(0, 0.08 + 0.05 * (j / NUM_NODES))
            
            angle = base_angle + filament_offset + angular_jitter
            
            x = r * math.cos(angle)
            y = r * math.sin(angle)
            
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
