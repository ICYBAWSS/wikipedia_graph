import os
import time
import json
import sqlite3
import requests
from datetime import datetime

# Configurations
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "wiki_cache.db"))
USER_AGENT = "WikiGraphBot/1.0 (repair-script; contact: rayhan@example.com)"
HEADERS = {"User-Agent": USER_AGENT}
WIKI_API_URL = "https://en.wikipedia.org/w/api.php"

# Meta pages prefixes to ignore
META_PREFIXES = (
    "Special:", "Wikipedia:", "Portal:", "Help:", "File:", "Category:", 
    "Talk:", "Template:", "User:", "Draft:", "MediaWiki:", "Module:", 
    "Gadget:", "Main Page", "Deaths in ", "List of ", "Lists of "
)

def get_article_details_api(title):
    """
    Fetches snippet, categories, and 30-day views for an article.
    Also paginates to retrieve ALL outbound links in namespace 0 (articles).
    Uses redirects=1 to ensure we get the actual article data.
    """
    meta_params = {
        "action": "query",
        "titles": title,
        "prop": "extracts|categories|pageviews",
        "exintro": True,
        "explaintext": True,
        "exsentences": 3,
        "cllimit": "max",
        "redirects": 1,
        "format": "json"
    }
    
    snippet = ""
    categories = []
    views_30d = 0
    
    try:
        r = requests.get(WIKI_API_URL, params=meta_params, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            pages = data.get("query", {}).get("pages", {})
            for page_id, page_info in pages.items():
                if page_id == "-1": # Page doesn't exist
                    return None
                snippet = page_info.get("extract", "")
                categories = [c["title"] for c in page_info.get("categories", [])]
                views = page_info.get("pageviews", {})
                views_30d = sum(v for v in views.values() if v is not None)
    except Exception as e:
        print(f"Error fetching metadata for {title}: {e}")
        return None

    links = []
    links_params = {
        "action": "query",
        "titles": title,
        "prop": "links",
        "plnamespace": 0,
        "pllimit": "max",
        "redirects": 1,
        "format": "json"
    }
    
    plcontinue = None
    while True:
        current_params = links_params.copy()
        if plcontinue:
            current_params["plcontinue"] = plcontinue
            
        try:
            r = requests.get(WIKI_API_URL, params=current_params, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                print(f"Error fetching links for {title}: HTTP {r.status_code}")
                break
                
            data = r.json()
            pages = data.get("query", {}).get("pages", {})
            for page_id, page_info in pages.items():
                for link in page_info.get("links", []):
                    link_title = link["title"]
                    if not any(link_title.startswith(prefix) for prefix in META_PREFIXES):
                        links.append(link_title)
            
            plcontinue = data.get("continue", {}).get("plcontinue")
            if not plcontinue:
                break
        except Exception as e:
            print(f"Error querying links loop for {title}: {e}")
            break
            
    return {
        "title": title,
        "snippet": snippet,
        "views": views_30d,
        "categories": categories,
        "links": list(set(links))
    }

def repair_missing_data(delay=0.5):
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Query for articles that are marked as crawled but have no content
    query = """
        SELECT title, views FROM articles 
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
    
    if not missing:
        print("No incomplete articles found to repair.")
        conn.close()
        return

    print(f"Starting repair for {len(missing)} articles...")
    
    for i, (title, old_views) in enumerate(missing):
        print(f"[{i+1}/{len(missing)}] Repairing: '{title}'...")
        
        details = get_article_details_api(title)
        
        if details is None:
            print(f"  - Failed to fetch data for '{title}' (skipping)")
            continue
            
        cursor.execute("""
            UPDATE articles 
            SET snippet = ?, views = ?, categories = ?, links = ?, last_updated = CURRENT_TIMESTAMP
            WHERE title = ?
        """, (
            details["snippet"],
            details["views"] if details["views"] > 0 else old_views,
            json.dumps(details["categories"]),
            json.dumps(details["links"]),
            title
        ))
        
        # Also ensure links are in the DB as seeds
        for link in details["links"]:
            cursor.execute("""
                INSERT OR IGNORE INTO articles (title, views, crawled)
                VALUES (?, 0, 0)
            """, (link,))
            
        conn.commit()
        time.sleep(delay)
        
    conn.close()
    print("Repair session finished!")

if __name__ == "__main__":
    repair_missing_data()
