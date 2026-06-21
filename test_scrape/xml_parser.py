import os
import re
import json
import sqlite3
import xml.etree.ElementTree as ET
import bz2
import time
import argparse
import subprocess

# Use the local test_scrape directory for the database
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "wiki_cache.db"))

def get_available_ram_gb():
    """Returns available RAM in GB for macOS."""
    try:
        vm = subprocess.check_output(['vm_stat']).decode()
        stats = {}
        for line in vm.split('\n')[1:]:
            if ':' in line:
                key, val = line.split(':')
                stats[key.strip()] = int(val.strip().strip('.')) * 4096 
        available = stats.get('Pages free', 0) + stats.get('Pages inactive', 0) + stats.get('Pages speculative', 0)
        return available / (1024**3)
    except:
        return 8.0

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    avail_gb = get_available_ram_gb()
    # Dedicated Mode: Use up to 70% of available RAM, capped at 16GB
    cache_gb = min(avail_gb * 0.70, 16.0)
    cache_kb = int(cache_gb * 1024 * 1024)
    
    print(f"System Memory: {avail_gb:.1f}GB available. Setting SQLite cache to {cache_gb:.1f}GB (DEDICATED MODE).")
    
    cursor.execute("PRAGMA journal_mode = OFF")
    cursor.execute("PRAGMA synchronous = OFF")
    cursor.execute(f"PRAGMA cache_size = -{cache_kb}")
    cursor.execute("PRAGMA temp_store = MEMORY")
    cursor.execute("PRAGMA locking_mode = EXCLUSIVE")
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            title TEXT PRIMARY KEY,
            snippet TEXT,
            views INTEGER,
            categories TEXT,
            links TEXT,
            link_contexts TEXT,
            wikidata_type TEXT,
            crawled INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def clean_wikitext(text):
    if not text: return ""
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    text = re.sub(r'<ref[^>/]*>.*?</ref>', '', text, flags=re.DOTALL)
    text = re.sub(r'<ref[^>]*/>', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\{\|.*?\|\}', '', text, flags=re.DOTALL)
    for _ in range(2): # Shallow template removal for speed
        text = re.sub(r'\{\{[^{}]*\}\}', '', text, flags=re.DOTALL)
    return text

def parse_wikitext_links(text):
    if not text: return [], {}
    
    # Fast regex for links
    raw_links = re.findall(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]', text)
    link_targets = set()
    links = []
    
    META_PREFIXES = ("Category:", "File:", "Image:", "Talk:", "Wikipedia:", "Portal:", "Help:", "Template:", "Special:", "MediaWiki:", "Module:")
    
    for target, anchor in raw_links:
        if any(target.startswith(prefix) for prefix in META_PREFIXES):
            continue
        target = target.split('#', 1)[0].strip()
        if not target: continue
        
        # Normalize
        if len(target) > 1: target = target[0].upper() + target[1:]
        elif len(target) == 1: target = target.upper()
        
        link_targets.add(target)
        links.append((target, anchor if anchor else target))

    # Context extraction (Surgical & Fast)
    link_contexts = {}
    if links:
        # Only take the first 2000 chars for context to save time/space
        sample_text = clean_wikitext(text[:3000])
        sentences = re.split(r'(?<=[.!?])\s+', sample_text)
        
        for target, anchor in links[:50]: # Limit links-per-article for performance
            for sentence in sentences:
                if anchor in sentence and 20 < len(sentence) < 300:
                    link_contexts[target] = " ".join(sentence.split())
                    break
    
    return list(link_targets), link_contexts

def load_pageviews(pageviews_path):
    views = {}
    print(f"Loading pageviews from {pageviews_path}...")
    # Pageviews are usually large, we stream them
    import gzip
    opener = bz2.open if pageviews_path.endswith('.bz2') else (gzip.open if pageviews_path.endswith('.gz') else open)
    
    with opener(pageviews_path, 'rt', encoding='utf-8', errors='ignore') as f:
        for i, line in enumerate(f):
            parts = line.strip().split()
            # Format: en.wikipedia Title Views
            if len(parts) >= 3 and (parts[0] == 'en' or parts[0] == 'en.wikipedia'):
                title = parts[1].replace('_', ' ')
                try:
                    count = int(parts[2])
                    views[title] = views.get(title, 0) + count
                except ValueError: continue
            if i % 10000000 == 0 and i > 0:
                print(f"  Processed {i/1e6:.0f}M pageview lines...")
    return views

def parse_xml_dump(xml_path, pageviews_dict=None, limit=None):
    init_db()
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Dedicated Mode: Use up to 70% of available RAM, capped at 16GB
    avail_gb = get_available_ram_gb()
    cache_kb = int(min(avail_gb * 0.70, 16.0) * 1024 * 1024)
    cursor.execute(f"PRAGMA cache_size = -{cache_kb}")
    cursor.execute("PRAGMA journal_mode = OFF")
    cursor.execute("PRAGMA synchronous = OFF")
    
    # Resume Logic: Check how many articles we already have
    cursor.execute("SELECT count(*) FROM articles")
    skip_count = cursor.fetchone()[0]
    print(f"Resuming: Already have {skip_count} articles in cache. Skipping ahead...")

    f = bz2.BZ2File(xml_path, 'rb')
    print(f"Streaming {xml_path}...")
    
    context = ET.iterparse(f, events=('end',))
    _, root = next(context)
    
    cursor.execute("PRAGMA journal_mode = OFF")
    cursor.execute("PRAGMA synchronous = OFF")
    
    batch = []
    count = 0
    start_time = time.time()
    
    for event, elem in context:
        tag = elem.tag.split('}')[-1]
        if tag == 'page':
            # Fast-track skipping
            if count < skip_count:
                count += 1
                if count % 100000 == 0:
                    print(f"  Skipped {count} articles...", end='\r')
                elem.clear()
                root.clear()
                continue

            try:
                ns = elem.findtext('{*}ns')
                title = elem.findtext('{*}title')
                
                if ns == '0' and title:
                    revision = elem.find('{*}revision')
                    text = revision.findtext('{*}text') if revision is not None else None
                    
                    if text and not text.strip().upper().startswith('#REDIRECT'):
                        links, contexts = parse_wikitext_links(text)
                        categories = re.findall(r'\[\[Category:([^\]|]+)', text)
                        
                        clean_body = clean_wikitext(text)
                        paragraphs = [p.strip() for p in clean_body.split('\n') if len(p.strip()) > 40]
                        snippet = paragraphs[0][:250] if paragraphs else ""
                        
                        views = pageviews_dict.get(title, 0) if pageviews_dict else 0
                        
                        batch.append((title, snippet, views, json.dumps(categories), 
                                     json.dumps(links), json.dumps(contexts), "", 1))
                        
                        if len(batch) >= 5000:
                            # Periodic Memory Health Check
                            avail = get_available_ram_gb()
                            if avail < 2.0: # If less than 2GB left, shrink cache
                                print(f"\nLow memory detected ({avail:.1f}GB available). Flushing cache...")
                                cursor.execute("PRAGMA shrink_memory")
                                
                            cursor.executemany("INSERT OR REPLACE INTO articles VALUES (?,?,?,?,?,?,?,?)", batch)
                            conn.commit()
                            batch = []
                            elapsed = time.time() - start_time
                            rate = (count - skip_count) / elapsed if elapsed > 0 else 0
                            print(f"  Processed {count} articles ({rate:.1f} art/s)...", end='\r')
                    
                count += 1
                if limit and count >= limit: break
            except Exception as e:
                print(f"\nError processing article {count}: {e}")
                continue
            finally:
                elem.clear()
                root.clear()
            
    if batch: cursor.executemany("INSERT OR REPLACE INTO articles VALUES (?,?,?,?,?,?,?,?)", batch)
    conn.commit()
    conn.close()
    f.close()
    print(f"\nFinished! Processed {count} articles total.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--views")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    
    views = load_pageviews(args.views) if args.views else None
    parse_xml_dump(args.file, views, args.limit)
