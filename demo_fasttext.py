import os
import sys
import sqlite3
import random
import subprocess
import json

# Ensure fasttext is installed (using fasttext-wheel for clean install on python 3.10+)
try:
    import fasttext
except ImportError:
    print("Installing fasttext-wheel...")
    subprocess.run([sys.executable, "-m", "pip", "install", "fasttext-wheel"])
    import fasttext

def create_synthetic_training_data():
    """Generates a small fasttext-formatted training file based on our category keywords."""
    print("Step 1: Creating synthetic training data...")
    
    # Category keywords matching our taxonomy
    topics = {
        "biography": [
            "was born in", "died in", "was an actor", "was a singer", "was a leader", 
            "served as president", "biography of", "celebrity star", "famous writer", "written by author"
        ],
        "science": [
            "study of physics", "biological cell", "chemical reaction", "mathematical equation", 
            "computer software programming", "space telescope launch", "medical drug discovery", "engineering system"
        ],
        "history": [
            "historical battle", "election vote campaign", "military force army", "political party government", 
            "ancient Roman empire", "civil war treaty", "industrial revolution history", "royal monarchy"
        ],
        "art": [
            "painted a portrait", "musical album release", "film movie actor", "television show series", 
            "art museum exhibition", "wrote a fiction novel", "theater play drama", "classic dance performance"
        ],
        "religion": [
            "philosophical theory", "religious belief faith", "mythological deity god", "christianity church temple", 
            "islam holy quran", "buddhism zen meditation", "sacred rituals worship", "metaphysical existence"
        ],
        "geography": [
            "located in the country", "capital city of", "mountain range height", "river flow sea", 
            "island continent area", "north south east west", "province region border", "coastal village lake"
        ]
    }
    
    train_file = "fasttext_train.txt"
    with open(train_file, "w") as f:
        # Generate variations to train the model
        for label, keywords in topics.items():
            for kw in keywords:
                # Add multiple synthetic sentences incorporating the keywords
                f.write(f"__label__{label} this article is about the {kw} in detail.\n")
                f.write(f"__label__{label} we explore the history and significance of {kw}.\n")
                f.write(f"__label__{label} research regarding {kw} has been published recently.\n")
                
    return train_file

def main():
    db_path = "test_scrape/wiki_cache.db"
    if not os.path.exists(db_path):
        print(f"Error: Database {db_path} not found.")
        sys.exit(1)
        
    # 1. Create and train model
    train_file = create_synthetic_training_data()
    print("Step 2: Training fasttext classifier on synthetic data...")
    model = fasttext.train_supervised(input=train_file, epoch=50, lr=0.5, wordNgrams=2)
    
    # 2. Get random articles
    print("Step 3: Fetching 10 random articles from SQLite cache...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Get random crawled articles that have snippets
    cursor.execute("""
        SELECT title, snippet, categories FROM articles 
        WHERE crawled = 1 AND snippet IS NOT NULL AND snippet != ''
        ORDER BY RANDOM() LIMIT 10
    """)
    rows = cursor.fetchall()
    conn.close()
    
    print("\n" + "="*80)
    print("FASTTEXT CLASSIFICATION RESULTS")
    print("="*80)
    
    for i, (title, snippet, categories_json) in enumerate(rows):
        # Clean text for fasttext (lowercase, remove newlines)
        clean_cats = ""
        if categories_json:
            try:
                cats = json.loads(categories_json)
                clean_cats = " ".join(cats)
            except: pass
            
        text_to_classify = f"{title} {snippet} {clean_cats}".lower().replace("\n", " ")
        
        # Predict
        labels, probabilities = model.predict(text_to_classify)
        
        # Format label (remove prefix)
        predicted_label = labels[0].replace("__label__", "")
        confidence = probabilities[0] * 100
        
        print(f"\n[{i+1}] Article: {title}")
        print(f"    Snippet: {snippet[:120]}...")
        print(f"    Predicted Topic: \033[92m{predicted_label.upper()}\033[0m ({confidence:.1f}% confidence)")
        
    print("="*80 + "\n")
    
    # 3. Clean up temporary training files
    print("Step 4: Cleaning up temporary files...")
    if os.path.exists(train_file):
        os.remove(train_file)
    print("Done!")

if __name__ == "__main__":
    main()
