import os
import time
import json
import sqlite3
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import re

# Configurations
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "wiki_cache.db"))
USER_AGENT = "WikiGraphResearchBot/2.0 (contact: rayhan@example.lan)"
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
            link_contexts TEXT,
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

def safe_request(url, params=None, headers=None, timeout=15, max_retries=5):
    """
    Wrapper for requests.get that handles 429 Too Many Requests with exponential backoff.
    """
    retries = 0
    while retries < max_retries:
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 429:
                wait_time = (2 ** retries) * 10
                print(f"  [Rate Limit] Hit 429. Waiting {wait_time}s before retry {retries + 1}/{max_retries}...")
                time.sleep(wait_time)
                retries += 1
                continue
            return r
        except Exception as e:
            print(f"  [Request Error] {e}. Retrying in 5s...")
            time.sleep(5)
            retries += 1
    return None

def get_wikidata_types_batch(titles):
    """
    Fetches Wikidata labels for multiple titles in bulk (up to 50).
    Returns a dictionary mapping title -> label.
    """
    if not titles: return {}
    
    # 1. Map Titles to QIDs
    params = {
        "action": "query",
        "prop": "pageprops",
        "ppprop": "wikibase_item",
        "titles": "|".join(titles),
        "redirects": 1,
        "format": "json"
    }
    
    title_to_qid = {}
    results = {t: "Other" for t in titles}

    try:
        r = safe_request(WIKI_API_URL, params=params, headers=HEADERS)
        if not r or r.status_code != 200: return results
        
        data = r.json()
        pages = data.get("query", {}).get("pages", {})
        for pid, pinfo in pages.items():
            t = pinfo.get("title")
            qid = pinfo.get("pageprops", {}).get("wikibase_item")
            if qid:
                # API might return title variations, check against original set
                for orig_t in titles:
                    if orig_t.lower() == t.lower():
                        title_to_qid[orig_t] = qid
                        break

        if not title_to_qid: return results

        # 2. Get P31 (Instance Of) for all QIDs
        qids = list(title_to_qid.values())
        wd_url = "https://www.wikidata.org/w/api.php"
        wd_params = {
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "props": "claims",
            "format": "json"
        }

        wr = safe_request(wd_url, params=wd_params, headers=HEADERS)
        if not wr or wr.status_code != 200: return results
        
        wdata = wr.json()
        entities = wdata.get("entities", {})
        
        qid_to_type_qid = {}
        for qid, einfo in entities.items():
            p31_claims = einfo.get("claims", {}).get("P31", [])
            if p31_claims:
                t_qid = p31_claims[0].get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
                if t_qid: qid_to_type_qid[qid] = t_qid

        if not qid_to_type_qid: return results

        # 3. Get labels for the type QIDs
        type_qids = list(set(qid_to_type_qid.values()))
        label_params = {
            "action": "wbgetentities",
            "ids": "|".join(type_qids[:50]),
            "props": "labels",
            "languages": "en",
            "format": "json"
        }
        
        lr = safe_request(wd_url, params=label_params, headers=HEADERS)
        if not lr or lr.status_code != 200: return results
        
        ldata = lr.json()
        type_entities = ldata.get("entities", {})
        type_qid_to_label = {}
        for t_qid, t_info in type_entities.items():
            label = t_info.get("labels", {}).get("en", {}).get("value")
            if label: type_qid_to_label[t_qid] = label

        # Final mapping
        for t, qid in title_to_qid.items():
            t_qid = qid_to_type_qid.get(qid)
            if t_qid:
                results[t] = type_qid_to_label.get(t_qid, "Other")

        return results

    except Exception as e:
        print(f"  [Wikidata Batch] Error: {e}")
        return results

def fetch_top_seeds():
    """
    Fetches the top popular articles from the Wikipedia Pageviews API.
    """
    today = datetime.now()
    first_day_current_month = today.replace(day=1)
    last_month = first_day_current_month - timedelta(days=15)
    year = last_month.strftime("%Y")
    month = last_month.strftime("%m")
    
    print(f"Fetching top articles from Pageviews API for {year}/{month}...")
    url = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{year}/{month}/all-days"
    
    try:
        r = safe_request(url, headers=HEADERS)
        if not r or r.status_code != 200:
            return []
        
        data = r.json()
        articles_list = data.get("items", [{}])[0].get("articles", [])
        
        seeds = []
        for art in articles_list:
            title = art["article"].replace("_", " ")
            views = art["views"]
            if any(title.startswith(prefix) for prefix in META_PREFIXES): continue
            if title.strip() == "": continue
            seeds.append({"title": title, "views": views})
        
        print(f"Acquired {len(seeds)} popular seed articles.")
        return seeds
    except Exception as e:
        print(f"Exception during Pageviews fetch: {e}")
        return []

def get_article_details_api(title):
    """
    Fetches snippet, categories, and 30-day views for an article.
    Discovers links ONLY from the descriptive text (ignoring footers/references).
    Extracts high-quality sentence context for each link.
    """
    MIN_WORDS = 10
    
    # 1. Fetch metadata
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
        r = safe_request(WIKI_API_URL, params=meta_params, headers=HEADERS)
        if r and r.status_code == 200:
            data = r.json()
            pages = data.get("query", {}).get("pages", {})
            for page_id, page_info in pages.items():
                if page_id == "-1": return None
                snippet = page_info.get("extract", "")
                categories = [c["title"] for c in page_info.get("categories", [])]
                views = page_info.get("pageviews", {})
                views_30d = sum(v for v in views.values() if v is not None)
        else:
            return None
    except Exception as e:
        print(f"  [Meta] Exception for {title}: {e}")
        return None

    # 2. Fetch HTML to discover links AND extract contexts
    parse_params = {
        "action": "parse",
        "page": title,
        "prop": "text",
        "redirects": 1,
        "format": "json"
    }
    
    discovered_links = set()
    link_contexts = {}
    
    try:
        r = safe_request(WIKI_API_URL, params=parse_params, headers=HEADERS, timeout=25)
        if r and r.status_code == 200:
            data = r.json()
            html_content = data.get("parse", {}).get("text", {}).get("*", "")
            if html_content:
                soup = BeautifulSoup(html_content, "html.parser")
                content = soup.find(class_="mw-parser-output") or soup
                
                # CLEANUP
                for junk in content.find_all(["div", "span"], class_=["reflist", "navbox", "sistersitebox", "mw-references-wrap", "reference"]):
                    junk.decompose()
                for header in content.find_all(["h2", "h3"]):
                    header_text = header.get_text().lower()
                    if any(x in header_text for x in ["references", "sources", "external links", "further reading", "notes"]):
                        for sibling in header.find_next_siblings():
                            sibling.decompose()
                        header.decompose()

                body_sentences = []
                for p in content.find_all("p"):
                    p_text = " ".join(p.get_text().split())
                    if not p_text: continue
                    sents = re.split(r'(?<=[.!?])\s+', p_text)
                    body_sentences.extend([s.strip() for s in sents if s.strip()])
                
                other_sentences = []
                for tag in content.find_all(["li", "td"]):
                    t_text = " ".join(tag.get_text().split())
                    if not t_text: continue
                    sents = re.split(r'(?<=[.!?])\s+', t_text)
                    other_sentences.extend([s.strip() for s in sents if s.strip()])

                for p in content.find_all("p"):
                    p_text = " ".join(p.get_text().split())
                    p_sentences = re.split(r'(?<=[.!?])\s+', p_text)
                    for a in p.find_all("a", href=True):
                        href = a["href"]
                        if href.startswith("/wiki/") and ":" not in href:
                            target_title = requests.utils.unquote(href.replace("/wiki/", "").replace("_", " "))
                            discovered_links.add(target_title)
                            
                            anchor_text = a.get_text().strip()
                            if not anchor_text: continue
                            for sentence in p_sentences:
                                if anchor_text in sentence:
                                    words = sentence.split()
                                    if len(words) >= MIN_WORDS:
                                        link_contexts[target_title] = sentence
                                        break
                
                for lt in discovered_links:
                    if lt not in link_contexts or len(link_contexts[lt].split()) < MIN_WORDS:
                        for sentence in body_sentences:
                            if lt in sentence and len(sentence.split()) >= MIN_WORDS:
                                link_contexts[lt] = sentence
                                break
                        if lt not in link_contexts or len(link_contexts[lt].split()) < MIN_WORDS:
                            for sentence in other_sentences:
                                if lt in sentence and len(sentence.split()) >= MIN_WORDS:
                                    link_contexts[lt] = sentence
                                    break
    except Exception as e:
        print(f"  [Parse] Exception for {title}: {e}")

    final_links = []
    final_contexts = {}
    for lt in discovered_links:
        final_links.append(lt)
        ctx = link_contexts.get(lt, "")
        clean_ctx = " ".join(ctx.split())
        if 10 < len(clean_ctx) < 600:
            final_contexts[lt] = clean_ctx
            
    return {
        "title": title,
        "snippet": snippet,
        "views": views_30d,
        "categories": categories,
        "links": final_links,
        "link_contexts": final_contexts
    }

def run_crawler(max_nodes=100, delay=1.0):
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check for seeds
    cursor.execute("SELECT COUNT(*) FROM articles")
    if cursor.fetchone()[0] == 0:
        seeds = fetch_top_seeds()
        for seed in seeds:
            cursor.execute("INSERT OR IGNORE INTO articles (title, views, crawled) VALUES (?, ?, 0)", (seed["title"], seed["views"]))
        conn.commit()
    
    # Get current progress
    cursor.execute("SELECT COUNT(*) FROM articles WHERE crawled = 1")
    crawled_count = cursor.fetchone()[0]
    print(f"Resuming crawl. Already finished: {crawled_count} / target {max_nodes}")
    
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 10

    while crawled_count < max_nodes:
        # Fetch next chunk of titles to process (for batching)
        batch_size = 20
        cursor.execute("SELECT title, views FROM articles WHERE crawled = 0 ORDER BY views DESC LIMIT ?", (batch_size,))
        rows = cursor.fetchall()
        
        if not rows:
            print("No more uncrawled articles in queue.")
            break
            
        # 1. Fetch metadata & HTML sequentially
        batch_results = []
        for title, views in rows:
            print(f"[{crawled_count + len(batch_results) + 1}/{max_nodes}] Crawling: '{title}'...")
            details = get_article_details_api(title)
            if details:
                batch_results.append((details, views))
                consecutive_errors = 0
            else:
                consecutive_errors += 1
                time.sleep(2)
            
            if len(batch_results) >= batch_size: break
            time.sleep(delay)

        # 2. Bulk fetch Wikidata types for the successful ones
        if batch_results:
            titles_for_wd = [r[0]["title"] for r in batch_results]
            print(f"  Fetching Wikidata types for batch of {len(titles_for_wd)}...")
            wiki_types = get_wikidata_types_batch(titles_for_wd)

            # 3. Commit batch to DB
            for details, views in batch_results:
                title = details["title"]
                wd_type = wiki_types.get(title, "Other")
                
                cursor.execute("""
                    UPDATE articles 
                    SET snippet = ?, views = ?, categories = ?, links = ?, 
                        link_contexts = ?, wikidata_type = ?,
                        crawled = 1, last_updated = CURRENT_TIMESTAMP
                    WHERE title = ?
                """, (
                    details["snippet"],
                    details["views"] if details["views"] > 0 else views,
                    json.dumps(details["categories"]),
                    json.dumps(details["links"]),
                    json.dumps(details["link_contexts"]),
                    wd_type,
                    title
                ))
                
                for link in details["links"]:
                    cursor.execute("INSERT OR IGNORE INTO articles (title, views, crawled) VALUES (?, 0, 0)", (link,))
                
                crawled_count += 1
            
            conn.commit()

        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            print("!!! Too many consecutive errors. Pausing.")
            break
        
    conn.close()
    print(f"Crawl session paused/finished. Total crawled articles: {crawled_count}")

if __name__ == "__main__":
    import sys
    limit = 100
    if len(sys.argv) > 1: limit = int(sys.argv[1])
    run_crawler(max_nodes=limit)
