import sqlite3
import json
import requests
import time
import re
from bs4 import BeautifulSoup

DB_PATH = "scraper/wiki_cache.db"
WIKI_API_URL = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "WikiGraphRepairBot/1.0"}

def get_link_contexts(title):
    parse_params = {
        "action": "parse",
        "page": title,
        "prop": "text",
        "redirects": 1,
        "format": "json"
    }
    
    link_contexts = {}
    try:
        r = requests.get(WIKI_API_URL, params=parse_params, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            html_content = data.get("parse", {}).get("text", {}).get("*", "")
            if html_content:
                soup = BeautifulSoup(html_content, "html.parser")
                for p in soup.find_all(["p", "li"]):
                    text = p.get_text()
                    sentences = re.split(r'(?<=[.!?])\s+', text)
                    for a in p.find_all("a", href=True):
                        href = a["href"]
                        if href.startswith("/wiki/") and ":" not in href:
                            link_title = href.replace("/wiki/", "").replace("_", " ")
                            link_title = requests.utils.unquote(link_title)
                            anchor_text = a.get_text()
                            for sentence in sentences:
                                if anchor_text in sentence:
                                    clean_sentence = " ".join(sentence.split())
                                    if 10 < len(clean_sentence) < 500:
                                        link_contexts[link_title] = clean_sentence
                                        break
        return link_contexts
    except Exception as e:
        print(f"Error for {title}: {e}")
        return None

def repair():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT title FROM articles WHERE crawled = 1 AND link_contexts IS NULL")
    titles = [row[0] for row in cursor.fetchall()]
    print(f"Found {len(titles)} articles to repair.")
    
    for i, title in enumerate(titles):
        print(f"[{i+1}/{len(titles)}] Repairing: {title}")
        contexts = get_link_contexts(title)
        if contexts is not None:
            cursor.execute("UPDATE articles SET link_contexts = ? WHERE title = ?", (json.dumps(contexts), title))
            conn.commit()
        time.sleep(0.05)
    
    conn.close()
    print("Repair complete!")

if __name__ == "__main__":
    repair()
