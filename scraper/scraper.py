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
            wikidata_id TEXT,
            wikidata_type TEXT,
            crawled INTEGER DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect(DB_PATH)

def get_wikidata_type(title):
    """
    Fetches the Wikidata QID and its 'Instance Of' (P31) label for an article.
    """
    params = {
        "action": "query",
        "prop": "pageprops",
        "ppprop": "wikibase_item",
        "titles": title,
        "redirects": 1,
        "format": "json"
    }

    try:
        r = requests.get(WIKI_API_URL, params=params, headers=HEADERS, timeout=10)
        data = r.json()
        pages = data.get("query", {}).get("pages", {})
        qid = None
        for pid in pages:
            qid = pages[pid].get("pageprops", {}).get("wikibase_item")
            if qid: break

        if not qid: return None, None

        # Now query Wikidata for the label of P31 (Instance Of)
        wd_url = "https://www.wikidata.org/w/api.php"
        wd_params = {
            "action": "wbgetentities",
            "ids": qid,
            "props": "claims",
            "format": "json"
        }

        wr = requests.get(wd_url, params=wd_params, headers=HEADERS, timeout=10)
        wdata = wr.json()
        claims = wdata.get("entities", {}).get(qid, {}).get("claims", {})

        # P31 = Instance Of
        p31_claims = claims.get("P31", [])
        if not p31_claims: return qid, "Other"

        # Get the label of the first instance_of target
        type_qid = p31_claims[0].get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
        if not type_qid: return qid, "Other"

        # Get label for that type_qid
        label_params = {
            "action": "wbgetentities",
            "ids": type_qid,
            "props": "labels",
            "languages": "en",
            "format": "json"
        }
        lr = requests.get(wd_url, params=label_params, headers=HEADERS, timeout=10)
        ldata = lr.json()
        label = ldata.get("entities", {}).get(type_qid, {}).get("labels", {}).get("en", {}).get("value", "Other")

        return qid, label

    except Exception as e:
        print(f"Error fetching Wikidata for {title}: {e}")
        return None, None

def get_article_details_api(title):
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

from bs4 import BeautifulSoup
import re

def get_article_details_api(title):
    """
    Fetches snippet, categories, and 30-day views for an article.
    Also paginates to retrieve ALL outbound links in namespace 0 (articles)
    and extracts the sentence context for each link.
    """
    # 1. Fetch metadata (snippet, categories, views)
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

    # 2. Fetch HTML to extract link contexts
    # We use action=parse to get the HTML of the first few sections or full page
    parse_params = {
        "action": "parse",
        "page": title,
        "prop": "text",
        "redirects": 1,
        "format": "json"
    }
    
    link_contexts = {}
    links = []
    
    try:
        r = requests.get(WIKI_API_URL, params=parse_params, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            html_content = data.get("parse", {}).get("text", {}).get("*", "")
            if html_content:
                soup = BeautifulSoup(html_content, "html.parser")
                # Find all links in the main content area (p, li, etc.)
                for p in soup.find_all(["p", "li"]):
                    text = p.get_text()
                    # Split into sentences (rough approximation)
                    sentences = re.split(r'(?<=[.!?])\s+', text)
                    
                    for a in p.find_all("a", href=True):
                        # Internal wiki links look like /wiki/Title
                        href = a["href"]
                        if href.startswith("/wiki/") and ":" not in href:
                            link_title = href.replace("/wiki/", "").replace("_", " ")
                            link_title = requests.utils.unquote(link_title)
                            
                            # Find which sentence contains this link's anchor text
                            anchor_text = a.get_text()
                            for sentence in sentences:
                                if anchor_text in sentence:
                                    # Clean up sentence (remove multiple spaces, etc.)
                                    clean_sentence = " ".join(sentence.split())
                                    if len(clean_sentence) > 10 and len(clean_sentence) < 500:
                                        link_contexts[link_title] = clean_sentence
                                        break
                
                links = list(link_contexts.keys())
    except Exception as e:
        print(f"Error fetching HTML/parsing links for {title}: {e}")

    # Fallback to prop=links if no links found via parse (unlikely for valid articles)
    if not links:
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
                if r.status_code != 200: break
                    
                data = r.json()
                pages = data.get("query", {}).get("pages", {})
                for page_id, page_info in pages.items():
                    for link in page_info.get("links", []):
                        link_title = link["title"]
                        if not any(link_title.startswith(prefix) for prefix in META_PREFIXES):
                            links.append(link_title)
                
                plcontinue = data.get("continue", {}).get("plcontinue")
                if not plcontinue: break
            except Exception: break
            
    return {
        "title": title,
        "snippet": snippet,
        "views": views_30d,
        "categories": categories,
        "links": list(set(links)),
        "link_contexts": link_contexts
    }

def run_crawler(max_nodes=1500, delay=0.1):
    """
    Main crawl loop.
    1. Check if we have seeds in the database. If not, fetch and insert.
    2. Pick the uncrawled article with the highest pageviews.
    3. Crawl details, outbound links, and contexts.
    4. Insert/update database.
    """
    init_db()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check if we need to add the link_contexts column
    try:
        cursor.execute("SELECT link_contexts FROM articles LIMIT 1")
    except sqlite3.OperationalError:
        print("Updating database schema: adding link_contexts column...")
        cursor.execute("ALTER TABLE articles ADD COLUMN link_contexts TEXT")
        conn.commit()
    
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
                SET crawled = 1, snippet = NULL, categories = NULL, links = NULL, link_contexts = NULL
                WHERE title = ?
            """, (title,))
            conn.commit()
            print(f"Skipped (not found or error): '{title}'")
            crawled_count += 1
            continue
            
        wd_id, wd_type = get_wikidata_type(title)
        
        cursor.execute("""
            UPDATE articles 
            SET snippet = ?, views = ?, categories = ?, links = ?, 
                link_contexts = ?, wikidata_id = ?, wikidata_type = ?,
                crawled = 1, last_updated = CURRENT_TIMESTAMP
            WHERE title = ?
        """, (
            details["snippet"],
            details["views"] if details["views"] > 0 else views,
            json.dumps(details["categories"]),
            json.dumps(details["links"]),
            json.dumps(details["link_contexts"]),
            wd_id,
            wd_type,
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
