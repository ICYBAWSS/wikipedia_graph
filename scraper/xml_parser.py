import os
import re
import json
import sqlite3
import xml.etree.ElementTree as ET
import bz2
import gzip
import time
import argparse

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "wiki_cache.db"))

def init_db():
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

def clean_wikitext(text):
    if not text:
        return ""
    # Remove HTML comments
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    # Remove ref tags
    text = re.sub(r'<ref[^>/]*>.*?</ref>', '', text, flags=re.DOTALL)
    text = re.sub(r'<ref[^>]*/>', '', text)
    # Remove other HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Remove tables
    text = re.sub(r'\{\|.*?\|\}', '', text, flags=re.DOTALL)
    # Remove templates
    for _ in range(3):
        text = re.sub(r'\{\{[^{}]*\}\}', '', text, flags=re.DOTALL)
    return text

LINK_RE = re.compile(r'\[\[([^\]]+)\]\]')
META_PREFIXES = ("Category:", "File:", "Image:", "Talk:", "Wikipedia:", "Portal:", "Help:", "Template:", "Special:", "MediaWiki:", "Module:")

def _normalize_target(raw_target):
    target = raw_target.split('#', 1)[0].strip()
    if not target:
        return None
    # Normalize: Wikipedia titles capitalize the first letter
    if len(target) > 1:
        return target[0].upper() + target[1:]
    return target.upper()

def parse_wikitext_links(text):
    """
    Parses wikitext links and extracts unique destination link titles and sentence contexts.
    """
    if not text:
        return [], {}

    # Full target list for the article, independent of context extraction: every
    # [[...]] occurrence anywhere in the text, regardless of which sentence it's in.
    link_targets = set()
    for m in LINK_RE.finditer(text):
        raw_target = m.group(1).split('|', 1)[0].strip()
        if any(raw_target.startswith(prefix) for prefix in META_PREFIXES):
            continue
        target = _normalize_target(raw_target)
        if target:
            link_targets.add(target)

    # Context extraction. Split the cleaned text into sentences with [[...]] markup
    # still intact, then only ever pull a target's context from a sentence that
    # contains that target's own literal [[...]] occurrence — never from some other
    # sentence elsewhere in the article that happens to contain the same word as
    # plain text. (The old approach stripped links to anchor text *before* splitting
    # and then searched every sentence in the whole article for the anchor as a
    # substring; for a common anchor like "Indian" that's shared with unrelated plain
    # text — e.g. "...is an Indian-American businesswoman..." — it would just as
    # happily grab a sentence that never linked anywhere, and did.)
    cleaned_text = clean_wikitext(text)
    sentences = re.split(r'(?<=[.!?])\s+', cleaned_text)

    def repl(match):
        parts = match.group(1).split('|', 1)
        return parts[1].strip() if len(parts) > 1 else parts[0].strip()

    link_contexts = {}
    for sentence in sentences:
        matches = list(LINK_RE.finditer(sentence))
        if not matches:
            continue

        display_sentence = LINK_RE.sub(repl, sentence)
        display_sentence = re.sub(r"'''+|''+", "", display_sentence)
        clean_sentence = " ".join(display_sentence.split())
        if not (10 < len(clean_sentence) < 500):
            continue

        for m in matches:
            raw_target = m.group(1).split('|', 1)[0].strip()
            if any(raw_target.startswith(prefix) for prefix in META_PREFIXES):
                continue
            target = _normalize_target(raw_target)
            if not target or target in link_contexts:
                continue
            link_contexts[target] = clean_sentence
                    
    return list(link_targets), link_contexts

def load_pageviews(pageviews_path):
    views = {}
    print(f"Loading pageviews from {pageviews_path}...")
    if pageviews_path.endswith('.gz'):
        opener = gzip.open(pageviews_path, 'rt', encoding='utf-8', errors='ignore')
    else:
        opener = open(pageviews_path, 'r', encoding='utf-8', errors='ignore')
        
    with opener as f:
        for i, line in enumerate(f):
            parts = line.strip().split()
            if len(parts) >= 3 and parts[0] == 'en':
                title = parts[1].replace('_', ' ')
                try:
                    count = int(parts[2])
                    # Take max if there are duplicates
                    views[title] = max(views.get(title, 0), count)
                except ValueError:
                    continue
            if i > 0 and i % 5000000 == 0:
                print(f"  Processed {i} raw pageviews lines...")
    print(f"Loaded {len(views)} pageview entries successfully.")
    return views

def parse_xml_dump(xml_path, pageviews_dict=None, limit=None):
    init_db()
    
    if xml_path.endswith('.bz2'):
        f = bz2.BZ2File(xml_path, 'rb')
    elif xml_path.endswith('.gz'):
        f = gzip.GzipFile(xml_path, 'rb')
    else:
        f = open(xml_path, 'rb')
        
    print(f"Starting parse of {xml_path}...")
    
    context = ET.iterparse(f, events=('end',))
    _, root = next(context)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    batch = []
    crawled_count = 0
    start_time = time.time()
    
    for event, elem in context:
        tag = elem.tag.split('}')[-1]
        if tag == 'page':
            # Extract namespace, skip if not main namespace (ns = 0)
            ns = None
            title = None
            text = None
            
            for child in elem:
                c_tag = child.tag.split('}')[-1]
                if c_tag == 'ns':
                    ns = child.text
                elif c_tag == 'title':
                    title = child.text
                elif c_tag == 'revision':
                    for subchild in child:
                        s_tag = subchild.tag.split('}')[-1]
                        if s_tag == 'text':
                            text = subchild.text
                            
            if ns == '0' and title and text:
                # Skip redirect pages
                if text.strip().upper().startswith('#REDIRECT'):
                    elem.clear()
                    root.clear()
                    continue
                    
                # Parse links, contexts, and categories
                links, contexts = parse_wikitext_links(text)
                
                # Extract category tags
                categories = []
                for cat in re.findall(r'\[\[Category:([^\]]+)\]\]', text):
                    # Remove sort keys if any (e.g. [[Category:Computing|Accessible]])
                    cat_name = cat.split('|', 1)[0].strip()
                    categories.append("Category:" + cat_name)
                    
                # Extract snippet
                clean_body = clean_wikitext(text)
                paragraphs = [p.strip() for p in clean_body.split('\n') if p.strip()]
                snippet = paragraphs[0] if paragraphs else ""
                if len(snippet) > 250:
                    snippet = snippet[:247] + "..."
                    
                # Get views from dict or default to 0
                views = pageviews_dict.get(title, 0) if pageviews_dict else 0
                
                batch.append((
                    title,
                    snippet,
                    views,
                    json.dumps(categories),
                    json.dumps(links),
                    json.dumps(contexts),
                    "", # wikidata_id placeholder
                    "", # wikidata_type placeholder
                    1   # crawled = 1 (complete)
                ))
                
                crawled_count += 1
                
                if len(batch) >= 5000:
                    cursor.executemany("""
                        INSERT OR REPLACE INTO articles 
                        (title, snippet, views, categories, links, link_contexts, wikidata_id, wikidata_type, crawled)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, batch)
                    conn.commit()
                    batch = []
                    
                    elapsed = time.time() - start_time
                    rate = crawled_count / elapsed
                    print(f"  Parsed {crawled_count} articles ({rate:.1f} articles/sec)...")
                    
                if limit and crawled_count >= limit:
                    break
                    
            elem.clear()
            root.clear()
            
    # Insert remaining records
    if batch:
        cursor.executemany("""
            INSERT OR REPLACE INTO articles 
            (title, snippet, views, categories, links, link_contexts, wikidata_id, wikidata_type, crawled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, batch)
        conn.commit()
        
    conn.close()
    f.close()
    
    total_time = time.time() - start_time
    print(f"Parsed {crawled_count} articles in {total_time:.1f}s.")
    print(f"Database saved to {DB_PATH}.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wikipedia XML Dump Parser to wiki_cache.db")
    parser.add_argument("--file", required=True, help="Path to enwiki pages-articles XML dump file (.xml, .bz2, or .gz)")
    parser.add_argument("--views", help="Path to enwiki pageviews file (.txt or .gz)")
    parser.add_argument("--limit", type=int, help="Limit the number of articles parsed")
    args = parser.parse_args()
    
    pageviews = None
    if args.views:
        pageviews = load_pageviews(args.views)
        
    parse_xml_dump(args.file, pageviews, args.limit)
