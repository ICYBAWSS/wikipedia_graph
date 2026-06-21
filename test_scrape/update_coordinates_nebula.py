import sqlite3
import math
import struct
import time
import os
import random

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "wiki_simulation.db"))
BIN_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "coordinates.bin"))
BATCH_SIZE = 200_000

def get_node_count():
    if not os.path.exists(DB_PATH):
        return 0
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) FROM nodes")
    count = cursor.fetchone()[0]
    conn.close()
    return count

def main():
    # Set random seed for reproducibility
    random.seed(42)
    
    num_nodes = get_node_count()
    if num_nodes == 0:
        print("Error: No nodes found in database.")
        return
        
    print(f"Processing {num_nodes} nodes...")
    num_categories = 7
    
    # Category color angles in radians to match:
    # Category 0 (Pink): bottom-left (250 degrees)
    # Category 1 (Blue): bottom-right (320 degrees)
    # Category 2 (Yellow/Orange): top-right (45 degrees)
    # Category 3 (Red): top-right (15 degrees)
    # Category 4 (Green): top-left (135 degrees)
    # Category 5 (Greenish-gray): bottom-left (210 degrees)
    # Category 6 (Purple): bottom-right (285 degrees)
    category_angles = {
        0: 250.0 * math.pi / 180.0,
        1: 320.0 * math.pi / 180.0,
        2: 45.0 * math.pi / 180.0,
        3: 15.0 * math.pi / 180.0,
        4: 135.0 * math.pi / 180.0,
        5: 210.0 * math.pi / 180.0,
        6: 285.0 * math.pi / 180.0
    }
    
    print("Step 1: Re-generating coordinates.bin with Nebula layout...")
    start_time = time.time()
    
    with open(BIN_PATH, "wb") as f:
        f.write(struct.pack("I", num_nodes))
        for j in range(num_nodes):
            cat_idx = j % num_categories
            base_angle = category_angles[cat_idx]
            
            # Filament branching (3 arms per category)
            filament_idx = (j // num_categories) % 3
            filament_offset = (filament_idx - 1) * 0.18
            
            # Distance from the top-center focal point
            base_r = 1.05 * (j**0.45)
            r = base_r * random.uniform(0.7, 1.3)
            
            # Angular Gaussian jitter
            angular_jitter = random.gauss(0, 0.08 + 0.05 * (j / num_nodes))
            
            angle = base_angle + filament_offset + angular_jitter
            
            # Local coordinates relative to top-center origin (0, 350)
            lx = r * math.cos(angle)
            ly = r * math.sin(angle)
            
            # Apply slight rotation (approx 15 degrees) to angle the entire mass from bottom-left to top-right
            rot_angle = 15.0 * math.pi / 180.0
            x = lx * math.cos(rot_angle) - ly * math.sin(rot_angle)
            y = 350.0 + (lx * math.sin(rot_angle) + ly * math.cos(rot_angle))
            
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
    
    for i in range(0, num_nodes, BATCH_SIZE):
        batch = []
        end = min(i + BATCH_SIZE, num_nodes)
        for j in range(i, end):
            cat_idx = j % num_categories
            base_angle = category_angles[cat_idx]
            
            filament_idx = (j // num_categories) % 3
            filament_offset = (filament_idx - 1) * 0.18
            
            base_r = 1.05 * (j**0.45)
            r = base_r * random.uniform(0.7, 1.3)
            
            angular_jitter = random.gauss(0, 0.08 + 0.05 * (j / num_nodes))
            
            angle = base_angle + filament_offset + angular_jitter
            
            lx = r * math.cos(angle)
            ly = r * math.sin(angle)
            
            rot_angle = 15.0 * math.pi / 180.0
            x = lx * math.cos(rot_angle) - ly * math.sin(rot_angle)
            y = 350.0 + (lx * math.sin(rot_angle) + ly * math.cos(rot_angle))
            
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
