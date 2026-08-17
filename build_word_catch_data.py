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

Decoys are ranked by semantic similarity to the topic (sentence-transformers
embeddings, build-time only), among candidates drawn from the topic's own
2-hop graph neighborhood -- that's what makes them read as plausible near
misses ("Tapir"/"Armadillo" under "Capybara") instead of unrelated popular
articles. A bare title embedded against a global "popular articles" pool
turned out too noisy on its own (biographies dominate high out-degree nodes,
so "Al Capp" outranked "Elephant" for "Capybara"); restricting candidates to
nodes structurally close to the topic first, then ranking that smaller set by
embedding similarity, fixes it. The global pool is kept only as a fallback
for topics whose 2-hop neighborhood is too thin.
"""
import json
import random
import re
import sqlite3
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
DB = ROOT / "wiki_graph_structure_full.db"
OUT = ROOT / "word_catch_data.json"
EMB_CACHE = ROOT / "word_catch_emb.npy"
EMB_TITLES_CACHE = ROOT / "word_catch_emb_titles.json"

REAL_PER_TOPIC = 10
DECOYS_PER_TOPIC = 18
MAX_TITLE_LEN = 16
MAX_JSON_BYTES = 150_000

POOL_SIZE = 50_000  # fallback candidate pool for decoys, ranked by out-degree
DECOY_RANK_LO = 2  # skip closest matches -- near-synonyms, not fun decoys
DECOY_RANK_HI = 60  # cap how far out we look -- keeps decoys thematically tight

TWO_HOP_SAMPLE = 40  # neighbors to expand from
TWO_HOP_LIMIT = 500  # links fetched per expanded neighbor
TWO_HOP_TOP_K = 600  # candidate nodes kept, by expansion frequency

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

    # Movies / pop culture
    "The Godfather", "Jurassic Park", "The Matrix", "Back to the Future",
    "Ghostbusters", "Shrek", "Toy Story", "The Lion King", "Godzilla",
    "King Kong", "Blade Runner", "Die Hard", "Home Alone",
    "Monty Python and the Holy Grail", "Groundhog Day (film)",
    "Casablanca (film)", "The Wizard of Oz", "Pulp Fiction", "Forrest Gump",
    "Fight Club", "Beetlejuice", "Jumanji", "Star Trek", "Doctor Who",
    "James Bond", "Dracula", "Frankenstein", "Sherlock Holmes",
    "SpongeBob SquarePants", "Looney Tunes", "Tom and Jerry", "Mickey Mouse",
    "Scooby-Doo",

    # Animals with inherent comedy
    "Capybara", "Axolotl", "Platypus", "Naked mole-rat", "Sloth", "Raccoon",
    "Flamingo", "Llama", "Alpaca", "Koala", "Kangaroo", "Hedgehog",
    "Jellyfish", "Snail", "Skunk", "Cockroach", "Goldfish", "Hamster",
    "Parrot",

    # Everyday absurd
    "Toilet paper", "Rubber duck", "Duct tape", "Bubble wrap",
    "Traffic cone", "Toaster", "Microwave oven", "Escalator",
    "Shopping cart", "Alarm clock", "Hot dog", "Doughnut", "Taco",
    "Marshmallow", "Pineapple", "Cheese", "Yo-yo", "Rubik's Cube", "Pinball",
    "Lego", "Karaoke", "Bowling", "Curling", "Sumo", "Darts", "Trampoline",
    "Roller coaster", "Juggling", "Circus", "Clown", "Mime artist",
    "Bagpipes", "Kazoo", "Yodeling", "Moustache", "Sneeze", "Hiccup",
    "Yawn", "Snoring", "Laughter", "Nap", "Insomnia", "Caffeine",
    "Procrastination", "Homework", "Bigfoot", "Loch Ness Monster", "Zombie",
    "Vampire", "Ninja", "Dragon", "Unicorn", "Garden gnome",
    "April Fools' Day",

    # Anime
    "Neon Genesis Evangelion", "Cowboy Bebop", "Akira (1988 film)",
    "Dragon Ball", "Dragon Ball Z", "Naruto", "One Piece", "Death Note",
    "Attack on Titan", "Fullmetal Alchemist", "Sailor Moon",
    "My Neighbor Totoro", "Princess Mononoke", "Spirited Away",
    "Ghost in the Shell", "Studio Ghibli", "Hayao Miyazaki", "Astro Boy",
    "Doraemon", "Gundam", "Mobile Suit Gundam", "Hunter × Hunter",
    "One-Punch Man", "Jujutsu Kaisen", "Demon Slayer: Kimetsu no Yaiba",
    "Chainsaw Man", "Bleach (manga)", "Perfect Blue", "Serial Experiments Lain",
    "Sword Art Online", "Anime", "Manga",
]


def clean_pool(cur, ids):
    """Filter a set of node ids down to short, non-junk titles -> {id: title}."""
    if not ids:
        return {}
    ids = list(ids)
    out = {}
    for i in range(0, len(ids), 900):  # SQLite bind-variable limit
        chunk = ids[i:i + 900]
        placeholders = ",".join("?" * len(chunk))
        rows = cur.execute(
            f"SELECT id, title FROM nodes WHERE id IN ({placeholders})", chunk
        ).fetchall()
        for nid, title in rows:
            if len(title) <= MAX_TITLE_LEN and not BAD_TITLE.search(title) and not title.isdigit():
                out[nid] = title
    return out


def near_dup(a, b):
    """True if a/b are trivially the same word (substring, or plural)."""
    a, b = a.lower(), b.lower()
    if a == b or a in b or b in a:
        return True
    if a.rstrip("s") == b.rstrip("s"):
        return True
    return False


def two_hop_candidate_ids(cur, topic_id, nbrs):
    """Nodes reachable in 2 hops from the topic, not already 1-hop neighbors.

    Structural closeness first, embedding similarity second -- this is what
    keeps decoys on-topic instead of "generically similar-sounding word".
    """
    cnt = Counter()
    sample = random.sample(list(nbrs), min(TWO_HOP_SAMPLE, len(nbrs))) if nbrs else []
    for nid in sample:
        rows = cur.execute(
            "SELECT target_idx FROM links WHERE source_idx=? LIMIT ?", (nid, TWO_HOP_LIMIT)
        ).fetchall()
        for (x,) in rows:
            if x != topic_id and x not in nbrs:
                cnt[x] += 1
    return [i for i, _ in cnt.most_common(TWO_HOP_TOP_K)]


def rank_and_sample(topic_title, cand_titles, cand_emb, topic_emb, exclude_lo, exclude_hi, n):
    """Given candidate titles + their embeddings, band-sample by similarity to topic_emb."""
    if not len(cand_titles):
        return []
    sims = cand_emb @ topic_emb
    order = np.argsort(sims)[::-1]

    ranked = [cand_titles[i] for i in order if not near_dup(cand_titles[i], topic_title)]

    band = ranked[exclude_lo:exclude_hi]
    if len(band) < n:
        band = ranked
    return random.sample(band, min(n, len(band)))


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    print("finding popular nodes (top out-degree)...")
    popular = [
        r[0]
        for r in cur.execute(
            "SELECT source_idx, COUNT(*) c FROM links "
            "GROUP BY source_idx ORDER BY c DESC LIMIT ?", (POOL_SIZE,)
        ).fetchall()
    ]
    popular_titles = clean_pool(cur, popular)
    pool_ids = list(popular_titles.keys())
    pool_titles = [popular_titles[i] for i in pool_ids]
    print(f"  {len(pool_ids)} usable pool nodes")

    from sentence_transformers import SentenceTransformer
    import torch

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = SentenceTransformer("all-MiniLM-L6-v2", device=device)

    print("loading/building title embeddings...")
    if EMB_CACHE.exists() and EMB_TITLES_CACHE.exists() and json.loads(EMB_TITLES_CACHE.read_text()) == pool_titles:
        pool_emb = np.load(EMB_CACHE)
        print(f"  loaded cached embeddings for {len(pool_titles)} titles")
    else:
        pool_emb = model.encode(
            pool_titles, normalize_embeddings=True, batch_size=512, show_progress_bar=True
        )
        np.save(EMB_CACHE, pool_emb)
        EMB_TITLES_CACHE.write_text(json.dumps(pool_titles))
        print(f"  embedded {len(pool_titles)} titles on {device}, cached to {EMB_CACHE.name}")

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
        # Outgoing + incoming links, UNFILTERED and UNLIMITED -- this is the
        # exclusion set for decoys, so it must not miss a real link just
        # because clean_pool() would reject its title or LIMIT 1000 cut it off.
        target_ids = [r[0] for r in cur.execute(
            "SELECT target_idx FROM links WHERE source_idx = ?", (topic_id,)
        ).fetchall()]
        source_ids = [r[0] for r in cur.execute(
            "SELECT source_idx FROM links WHERE target_idx = ?", (topic_id,)
        ).fetchall()]
        nbrs = set(target_ids + source_ids)

        real_titles = clean_pool(cur, nbrs)
        if len(real_titles) < REAL_PER_TOPIC:
            print(f"Warning: Topic '{topic_title}' has only {len(real_titles)} clean links, skipping.")
            continue
        real_sample = random.sample(list(real_titles.values()), REAL_PER_TOPIC)

        topic_emb = model.encode([topic_title], normalize_embeddings=True)[0]

        # Primary: candidates structurally close to the topic (2-hop), ranked
        # by embedding similarity within that set.
        two_hop_ids = two_hop_candidate_ids(cur, topic_id, nbrs)
        two_hop_titles_map = clean_pool(cur, two_hop_ids)
        cand_titles = [two_hop_titles_map[i] for i in two_hop_ids if i in two_hop_titles_map]
        cand_emb = model.encode(cand_titles, normalize_embeddings=True, batch_size=256) if cand_titles else np.empty((0, topic_emb.shape[0]))

        decoy_sample = rank_and_sample(topic_title, cand_titles, cand_emb, topic_emb, DECOY_RANK_LO, DECOY_RANK_HI, DECOYS_PER_TOPIC)

        # Fallback: thin 2-hop neighborhood (e.g. niche anime topics) -- widen
        # to the global popular-articles pool (already embedded) instead of
        # shipping too few decoys.
        if len(decoy_sample) < DECOYS_PER_TOPIC:
            mask = np.array([pool_ids[row] != topic_id and pool_ids[row] not in nbrs for row in range(len(pool_ids))])
            fallback_titles = [pool_titles[row] for row in range(len(pool_ids)) if mask[row]]
            fallback_emb = pool_emb[mask]
            fallback_sample = rank_and_sample(
                topic_title, fallback_titles, fallback_emb, topic_emb, DECOY_RANK_LO, DECOY_RANK_HI,
                DECOYS_PER_TOPIC - len(decoy_sample)
            )
            seen = set(decoy_sample)
            decoy_sample += [t for t in fallback_sample if t not in seen]

        topics.append({"topic": topic_title, "real": real_sample, "decoys": decoy_sample})

    # Shuffle the topics list to serialize in a random order
    random.shuffle(topics)

    payload = json.dumps(topics, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_JSON_BYTES:
        print(f"payload {len(payload)/1024:.1f} KB over budget, trimming decoys to 12/topic...")
        for t in topics:
            t["decoys"] = t["decoys"][:12]
        payload = json.dumps(topics, ensure_ascii=False, separators=(",", ":"))

    OUT.write_text(payload)
    print(f"wrote {OUT.name}: {len(topics)} topics, {len(payload) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
