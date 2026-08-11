# Wikipedia Graph Dataset - Hugging Face Deployments

All large database, cache, and raw dump files for the Wikipedia Graph Visualizer project have been deployed to the Hugging Face Hub.

* **Repository:** [icybawss/wikipedia-graph-data](https://huggingface.co/datasets/icybawss/wikipedia-graph-data)
* **Status:** Public
* **Last Updated:** July 6, 2026

---

## 📁 Deployed Files

| File Path in Repo | Size | Description |
| :--- | :--- | :--- |
| `wiki_graph_structure.db` | **3.12 GB** | Structured graph database containing the compiled `nodes` and `links` tables (without FTS indices). |
| `test_scrape/wiki_simulation.db` | **25.30 GB** | The primary graph database containing FTS, nodes, links, and contexts. **Expected by the WebGL Visualizer** (`8m_optimized.html`) via HTTP Range Requests / SQLite VFS. |
| `test_scrape/wiki_graph.db` | **25.30 GB** | Compiled graph database (duplicate of `wiki_simulation.db` for fallback/naming consistency). |
| `test_scrape/wiki_cache.db` | **21.77 GB** | Scraper cache database containing crawled and processed raw wikitext articles. |
| `test_scrape/enwiki-latest-pages-articles-multistream.xml.bz2` | **24.32 GB** | Raw multistream XML dump downloaded from Wikimedia. |
| `test_scrape/pageviews.bz2` | **5.86 GB** | Raw monthly user pageview counts dump downloaded from Wikimedia. |
| `edges_weighted.csv.gz` | **1.25 GB** | Log-normalized and weighted edge connections for layout physics processing. |
| `metadata.csv` | **138.97 MB** | CSV containing node titles, parent category IDs, and views metrics. |

---

## 📥 How to Access Files

### 1. Download via Hugging Face CLI (`hf`)
If the `hf` command-line client is installed, you can download files directly:

```bash
# Download a specific file (e.g., the simulation database)
hf download icybawss/wikipedia-graph-data test_scrape/wiki_simulation.db --local-dir .

# Download the entire repository
hf download icybawss/wikipedia-graph-data --local-dir wikipedia_graph_data
```

### 2. Download via Python SDK (`huggingface_hub`)
To stream or retrieve files programmatically inside python scripts:

```python
from huggingface_hub import hf_hub_download

# Download the simulation database
db_path = hf_hub_download(
    repo_id="icybawss/wikipedia-graph-data",
    filename="test_scrape/wiki_simulation.db",
    repo_type="dataset"
)
print(f"Downloaded database to: {db_path}")
```

### 3. Direct Streaming / VFS Range Requests URL
For streaming node details dynamically on GitHub Pages or locally without downloading the full 25.3 GB database:

```url
https://huggingface.co/datasets/icybawss/wikipedia-graph-data/resolve/main/test_scrape/wiki_simulation.db
```
*(Ensure range requests are supported by Hugging Face's CDN when pointing the visualizer's HTTP VFS worker code to this URL).*
