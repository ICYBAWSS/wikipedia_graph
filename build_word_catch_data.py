#!/usr/bin/env python3
"""
Precompute the answer key for the loading-screen "word catch" minigame.

Why offline: the game has to work before the CSR (~500MB) finishes loading,
so "is this word actually linked from the core topic" can't be a live graph
query. Instead we bake a small JSON of {topic, real links, decoy words} pulled
straight from the same source-of-truth DB the CSR itself is built from
(wiki_graph_structure_full.db), using node id == the same 0..N-1 index used by
viewer_v2/titles_v2/CSR everywhere else in the app.

Picks core topics from well-linked (popular) nodes so the game reads as
"things you'd recognize," and filters both real links and decoys to
reasonably short, non-disambiguation titles so they render cleanly as falling
words.
"""
import json
import random
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "wiki_graph_structure_full.db"
OUT = ROOT / "word_catch_data.json"

REAL_PER_TOPIC = 10
DECOYS_PER_TOPIC = 18
MAX_TITLE_LEN = 16

BAD_TITLE = re.compile(r"\(disambiguation\)|^List of|^Category:|^Template:|^Wikipedia:|\b\d{4}\b|\(number\)", re.I)

# Hand-picked instead of "top out-degree" -- the highest-linked articles on
# Wikipedia skew hard toward "20XX in music"/"YYYY deaths"-style index pages,
# which are real, well-linked, and completely un-fun as a game topic. This
# list is core topics people actually recognize, still popular/well-linked
# enough to have a rich real-link pool.
CORE_TOPICS = [
    "Albert Einstein", "Leonardo da Vinci", "William Shakespeare", "Isaac Newton",
    "Charles Darwin", "Napoleon", "Abraham Lincoln", "Cleopatra", "Julius Caesar",
    "Marie Curie", "Nikola Tesla", "Mahatma Gandhi", "Martin Luther King Jr.",
    "Nelson Mandela", "Winston Churchill", "Elon Musk", "Steve Jobs",
    "Michael Jackson", "The Beatles", "Elvis Presley", "Taylor Swift",
    "Video game", "Chess", "Basketball", "Football", "Olympic Games",
    "World War II", "World War I", "Ancient Rome", "Ancient Egypt",
    "Ancient Greece", "Renaissance", "Cold War", "American Civil War",
    "Solar System", "Black hole", "DNA", "Evolution", "Gravity",
    "Quantum mechanics", "Artificial intelligence", "Internet", "Robot",
    "Dinosaur", "Volcano", "Earthquake", "Tropical cyclone", "Shark", "Lion",
    "Elephant", "Dolphin", "Penguin", "Octopus", "Wolf", "Tiger",
    "Amazon rainforest", "Sahara", "Mount Everest", "Great Barrier Reef",
    "Pacific Ocean", "Antarctica", "Japan", "France", "Egypt", "Brazil",
    "India", "China", "United Kingdom", "Canada", "Australia",
    "New York City", "Paris", "Tokyo", "Rome", "London",
    "Pizza", "Chocolate", "Coffee", "Sushi", "Star Wars", "Harry Potter",
    "The Lord of the Rings", "Marvel Comics", "Batman", "Superman",
    "Minecraft", "Super Mario", "Pokémon", "The Simpsons", "Jazz",
    "Rock music", "Hip-hop", "Classical music", "Piano", "Guitar",
    "Photography", "Painting", "Ballet", "Formula One", "Tennis",
    "Boxing", "Cricket", "Rugby", "Yoga", "Meditation",
]


def clean_pool(cur, ids):
    """Filter a set of node ids down to short, non-junk titles -> {id: title}."""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = cur.execute(
        f"SELECT id, title FROM nodes WHERE id IN ({placeholders})", list(ids)
    ).fetchall()
    out = {}
    for nid, title in rows:
        if len(title) <= MAX_TITLE_LEN and not BAD_TITLE.search(title) and not title.isdigit():
            out[nid] = title
    return out


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    print("finding popular nodes (top out-degree)...")
    popular = [
        r[0]
        for r in cur.execute(
            "SELECT source_idx, COUNT(*) c FROM links "
            "GROUP BY source_idx ORDER BY c DESC LIMIT 4000"
        ).fetchall()
    ]
    popular_titles = clean_pool(cur, popular)
    popular_pool = list(popular_titles.items())
    print(f"  {len(popular_pool)} usable popular nodes")

    # Find IDs for our CORE_TOPICS
    placeholders = ",".join("?" * len(CORE_TOPICS))
    topic_rows = cur.execute(
        f"SELECT id, title FROM nodes WHERE title IN ({placeholders})", CORE_TOPICS
    ).fetchall()
    topic_map = {title: nid for nid, title in topic_rows}

    candidate_topics = []
    for title in CORE_TOPICS:
        if title in topic_map:
            candidate_topics.append((topic_map[title], title))
        else:
            print(f"Warning: Core topic '{title}' not found in database.")

    topics = []
    for topic_id, topic_title in candidate_topics:
        # Outgoing links
        target_ids = [
            r[0]
            for r in cur.execute(
                "SELECT target_idx FROM links WHERE source_idx = ? LIMIT 1000", (topic_id,)
            ).fetchall()
        ]
        # Incoming links (as a fallback/addition for richer game topics)
        source_ids = [
            r[0]
            for r in cur.execute(
                "SELECT source_idx FROM links WHERE target_idx = ? LIMIT 1000", (topic_id,)
            ).fetchall()
        ]

        combined_ids = set(target_ids + source_ids)
        real_titles = clean_pool(cur, combined_ids)
        if len(real_titles) < REAL_PER_TOPIC:
            print(f"Warning: Topic '{topic_title}' has only {len(real_titles)} clean links, skipping.")
            continue
        real_sample = random.sample(list(real_titles.values()), REAL_PER_TOPIC)

        real_id_set = set(real_titles.keys())
        decoy_candidates = [t for i, t in popular_pool if i != topic_id and i not in real_id_set]
        decoy_sample = random.sample(decoy_candidates, DECOYS_PER_TOPIC)

        topics.append({"topic": topic_title, "real": real_sample, "decoys": decoy_sample})

    # Shuffle the topics list to serialize in a random order
    random.shuffle(topics)

    OUT.write_text(json.dumps(topics, ensure_ascii=False, separators=(",", ":")))
    print(f"wrote {OUT.name}: {len(topics)} topics, {OUT.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
