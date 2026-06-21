import sqlite3
import math
import struct
import time
import os

DB_PATH = "wiki_simulation.db"
BIN_PATH = "coordinates.bin"
NUM_NODES = 8_000_000
BATCH_SIZE = 200_000

def main():
    phi = (1 + 5**0.5) / 2
    scale = 0.6180339887
    
    print("Step 1: Re-generating coordinates.bin directly from formula...")
    start_time = time.time()
    
    with open(BIN_PATH, "wb") as f:
        f.write(struct.pack("I", NUM_NODES))
        for j in range(NUM_NODES):
            angle = 2 * math.pi * phi * j
            r = scale * (j**0.5)
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
    
    for i in range(0, NUM_NODES, BATCH_SIZE):
        batch = []
        end = min(i + BATCH_SIZE, NUM_NODES)
        for j in range(i, end):
            angle = 2 * math.pi * phi * j
            r = scale * (j**0.5)
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
