import os
import time
import json
import sqlite3
import requests
from datetime import datetime, timedelta

# Configurations
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "wiki_cache.db"))
USER_AGENT = "WikiGraphBot/1.0 (contact: rayhan@example.com)"
HEADERS = {"User-Agent": USER_AGENT}
WIKI_API_URL = "https://en.wikipedia.org/w/api.php"

# Meta pages prefixes to ignore
META_PREFIXES = (
    "Special:", "Wikipedia:", "Portal:", "Help:", "File:", "Category:", 
    "Talk:", "Template:", "User:", "Draft:", "MediaWiki:", "Module:", 
    "Gadget:", "Main Page", "Deaths in ", "List of ", "Lists of "
)

def init_db():
    """Initializes the SQLite database and creates the articles table if it doesn't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            title TEXT PRIMARY KEY,
            snippet TEXT,
            views INTEGER,
            categories TEXT,
            links TEXT,
            crawled INTEGER DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect(DB_PATH)

def fetch_top_seeds():
    """
    Fetches the top popular articles from the Wikipedia Pageviews API.
    We target the last full calendar month.
    """
    today = datetime.now()
    first_day_current_month = today.replace(day=1)
    last_month = first_day_current_month - timedelta(days=15) # middle of previous month
    year = last_month.strftime("%Y")
    month = last_month.strftime("%m")
    
    print(f"Fetching top articles from Pageviews API for {year}/{month}...")
    url = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{year}/{month}/all-days"
    
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"Error fetching pageviews: HTTP {r.status_code}")
            return []
        
        data = r.json()
        articles_list = data.get("items", [{}])[0].get("articles", [])
        
        seeds = []
        for art in articles_list:
            title = art["article"].replace("_", " ")
            views = art["views"]
            
            # Filter out non-article pages
            if any(title.startswith(prefix) for prefix in META_PREFIXES):
                continue
            if title.strip() == "":
                continue
            
            seeds.append({"title": title, "views": views})
        
        print(f"Acquired {len(seeds)} popular seed articles after filtering.")
        return seeds
    except Exception as e:
        print(f"Exception during Pageviews fetch: {e}")
        return []

def get_article_details_api(title):
    """
    Fetches snippet, categories, and 30-day views for an article.
    Also paginates to retrieve ALL outbound links in namespace 0 (articles).
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

def run_crawler(max_nodes=1500, delay=0.1):
    """
    Main crawl loop.
    1. Check if we have seeds in the database. If not, fetch and insert.
    2. Repeatedly pick the uncrawled article with the highest pageviews.
    3. Crawl details and outbound links.
    4. Insert/update database.
    """
    init_db()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM articles")
    count = cursor.fetchone()[0]
    
    if count == 0:
        print("Database is empty. Fetching seeds...")
        seeds = fetch_top_seeds()
        if not seeds:
            print("Failed to fetch seeds. Exiting.")
            conn.close()
            return
            
        for seed in seeds:
            cursor.execute("""
                INSERT OR IGNORE INTO articles (title, views, crawled)
                VALUES (?, ?, 0)
            """, (seed["title"], seed["views"]))
        conn.commit()
        print(f"Seeded database with {len(seeds)} popular entries.")
    
    cursor.execute("SELECT COUNT(*) FROM articles WHERE crawled = 1")
    crawled_count = cursor.fetchone()[0]
    print(f"Already crawled articles: {crawled_count} / {max_nodes}")
    
    while crawled_count < max_nodes:
        cursor.execute("""
            SELECT title, views FROM articles 
            WHERE crawled = 0 
            ORDER BY views DESC LIMIT 1
        """)
        row = cursor.fetchone()
        
        if not row:
            print("No more uncrawled articles in the queue. Crawl complete.")
            break
            
        title, views = row
        print(f"[{crawled_count + 1}/{max_nodes}] Crawling: '{title}' (Queue views: {views})...")
        
        details = get_article_details_api(title)
        
        if details is None:
            cursor.execute("""
                UPDATE articles 
                SET crawled = 1, snippet = NULL, categories = NULL, links = NULL
                WHERE title = ?
            """, (title,))
            conn.commit()
            print(f"Skipped (not found or error): '{title}'")
            crawled_count += 1
            continue
            
        cursor.execute("""
            UPDATE articles 
            SET snippet = ?, views = ?, categories = ?, links = ?, crawled = 1, last_updated = CURRENT_TIMESTAMP
            WHERE title = ?
        """, (
            details["snippet"],
            details["views"] if details["views"] > 0 else views,
            json.dumps(details["categories"]),
            json.dumps(details["links"]),
            title
        ))
        
        for link in details["links"]:
            cursor.execute("""
                INSERT OR IGNORE INTO articles (title, views, crawled)
                VALUES (?, 0, 0)
            """, (link,))
            
        conn.commit()
        crawled_count += 1
        
        time.sleep(delay)
        
    conn.close()
    print("Crawl session finished!")

if __name__ == "__main__":
    import sys
    limit = 100
    if len(sys.argv) > 1:
        limit = int(sys.argv[1])
    run_crawler(max_nodes=limit)
