import sqlite3
import os

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "wiki_cache.db"))

def find_missing_data():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Query for articles that are marked as crawled but have no content
    query = """
        SELECT title FROM articles 
        WHERE crawled = 1 
        AND (
            snippet IS NULL 
            OR snippet = '' 
            OR categories IS NULL 
            OR categories = '[]' 
            OR categories = ''
        )
    """
    
    cursor.execute(query)
    missing = cursor.fetchall()
    
    print(f"--- Database Audit ---")
    print(f"Total articles marked as 'crawled' but missing data: {len(missing)}")
    print("-" * 20)
    
    for row in missing:
        print(row[0])
        
    conn.close()
    
    if len(missing) > 0:
        print("-" * 20)
        print(f"Found {len(missing)} incomplete articles. Run 'repair_missing.py' to fix them.")
    else:
        print("All crawled articles have valid data!")

if __name__ == "__main__":
    find_missing_data()
