---
title: Wikipedia Link Graph and Layout Dataset (2026)
emoji: 🌌
colorFrom: indigo
colorTo: purple
sdk: static
pretty_name: Wikipedia Link Graph & Layout Dataset
dataset_info:
  features:
  - name: title
    dtype: string
  - name: idx
    dtype: int64
  - name: category
    dtype: int64
  - name: views
    dtype: int64
  - name: x
    dtype: float64
  - name: y
    dtype: float64
  splits:
  - name: train
    num_examples: 5483256
tags:
- graph
- wikipedia
- webgl
- sqlite
- rapids
- network
- link-prediction
- community-detection
- representation-learning
size_categories:
- 10M-100M
---

# Wikipedia Link Graph, layout, and Contexts Dataset

This repository hosts the complete, high-fidelity graph dataset of the **English Wikipedia** (approx. **5.48M articles/nodes** and **100M+ links/edges**). It is designed to enable researchers, developers, and graph-database enthusiasts to study massive web graphs, run node classification, representation learning (Node2Vec, GNNs), and explore spatial force-directed graph layouts.

This data directly backs the **Wikipedia Graph Visualizer**, an interactive cosmic WebGL space showing Wikipedia as a stellar galaxy.

* **GitHub Repository:** [ICYBAWSS/wikipedia_graph](https://github.com/ICYBAWSS/wikipedia_graph)
* **Interactive Visualizer:** [Live Demo](https://icybawss.github.io/wikipedia_graph/)

---

## 📁 File Structure & Specifications

The dataset includes raw dumps, processed structured databases, optimized binary indices, and graph edge lists.

| File Path in Repo | Size | Format | Description |
| :--- | :--- | :--- | :--- |
| `wiki_graph_structure.db` | **3.12 GB** | SQLite | Clean relational database of `nodes` and `links` tables. Ideal for general-purpose SQL queries. |
| `test_scrape/wiki_simulation.db` | **25.30 GB** | SQLite | **Production Database**. Contains the node graph, full-text search indexes (`fts_idx`), and wikitext snippets surrounding links (`contexts` table) used by the visualizer. |
| `test_scrape/wiki_graph.db` | **25.30 GB** | SQLite | Duplicate of `wiki_simulation.db` (retained for pipeline naming consistency). |
| `test_scrape/wiki_cache.db` | **21.77 GB** | SQLite | Crawled and processed raw wikitext articles from the pipeline scraper. |
| `test_scrape/enwiki-latest-pages-articles-multistream.xml.bz2` | **24.32 GB** | BZ2 | Raw XML Wikipedia multistream dump from Wikimedia. |
| `test_scrape/pageviews.bz2` | **5.86 GB** | BZ2 | Raw monthly user pageview counts dump from Wikimedia. |
| `edges_weighted.csv.gz` | **1.25 GB** | CSV (GZIP) | Tabular list of source and target node indices with weights. Useful for deep learning/GNN imports. |
| `metadata.csv` | **138.97 MB** | CSV | Tabular index of node indices, titles, parent category IDs, and views metrics. |
| `adjacency_csr.bin.gz` | **248.76 MB** | Binary (GZIP) | Packed Compressed Sparse Row (CSR) representation of out-edges for fast traversal. |
| `adjacency_csr_rev.bin.gz` | **252.27 MB** | Binary (GZIP) | Packed Compressed Sparse Row (CSR) representation of in-edges (incoming links). |
| `viewer_v2.bin.gz` | **35.18 MB** | Binary (GZIP) | Packed client-side array containing node indices, quantized coordinates, and node sizes. |
| `titles_v2.bin.gz` | **47.63 MB** | Binary (GZIP) | Sequentially concatenated UTF-8 title byte index for zero-cost offset lookups. |

---

## 🏛️ Schema Definitions (SQLite)

### 1. `wiki_graph_structure.db` (Clean Schema)

This SQLite database contains the core relational schemas:

* **`nodes` Table:**
  ```sql
  CREATE TABLE nodes (
      idx INTEGER PRIMARY KEY,   -- 0-indexed node sequence ID
      title TEXT UNIQUE,         -- Wikipedia Article Name (UTF-8)
      category INTEGER,          -- Wikipedia Category ID mapping
      views INTEGER,             -- Monthly Pageviews count
      x INTEGER,                 -- Force-directed X coordinate (quantized uint16)
      y INTEGER                  -- Force-directed Y coordinate (quantized uint16)
  );
  CREATE INDEX idx_nodes_title ON nodes(title);
  ```

* **`links` Table:**
  ```sql
  CREATE TABLE links (
      source_idx INTEGER,        -- Source node idx
      target_idx INTEGER,        -- Target node idx
      FOREIGN KEY(source_idx) REFERENCES nodes(idx),
      FOREIGN KEY(target_idx) REFERENCES nodes(idx)
  );
  CREATE INDEX idx_links_source ON links(source_idx);
  CREATE INDEX idx_links_target ON links(target_idx);
  ```

### 2. `test_scrape/wiki_simulation.db` (Visualizer Backend Schema)

This production database expands on the clean schema with full-text search indexes and link wikitext context snippets:

* **`contexts` Table:**
  ```sql
  CREATE TABLE contexts (
      source_idx INTEGER,        -- Source node idx
      target_idx INTEGER,        -- Target node idx
      context TEXT,              -- exact raw wikitext sentence containing the hyperlink
      PRIMARY KEY (source_idx, target_idx)
  );
  ```

---

## ⚡ Loading & Access Examples

### 1. SQLite Query Examples
To find the shortest paths or navigate link hierarchies, query the SQLite database locally or stream it:

```sql
-- Get the out-links (pages mentioned in the article 'SpaceX')
SELECT n.title 
FROM links l 
JOIN nodes n ON l.target_idx = n.idx 
WHERE l.source_idx = (SELECT idx FROM nodes WHERE title = 'SpaceX');

-- Get the in-links (pages linking back to 'Artificial intelligence')
SELECT n.title 
FROM links l 
JOIN nodes n ON l.source_idx = n.idx 
WHERE l.target_idx = (SELECT idx FROM nodes WHERE title = 'Artificial intelligence');

-- Find context wikitext snippet explaining a connection
SELECT context 
FROM contexts 
WHERE source_idx = (SELECT idx FROM nodes WHERE title = 'Python (programming language)')
  AND target_idx = (SELECT idx FROM nodes WHERE title = 'C++');
```

### 2. Streaming via HTTP Range Requests (SQLite VFS)
Because downloading the full `25.30 GB` database is impractical in the browser, the visualizer uses `sql-httpvfs` to stream chunks of the database directly from Hugging Face on-demand.

#### Javascript/HTML integration:
```javascript
import { createDbWorker } from "sql-httpvfs";

const workerUrl = new URL("sqlite.worker.js", import.meta.url).href;
const wasmUrl = new URL("sql-wasm.wasm", import.meta.url).href;

const dbUrl = "https://huggingface.co/datasets/icybawss/wikipedia-graph-data/resolve/main/test_scrape/wiki_simulation.db";

const worker = await createDbWorker(
  [
    {
      from: "inline",
      config: {
        serverMode: "full",
        requestChunkSize: 65536, // 64KB range queries
        url: dbUrl
      }
    }
  ],
  workerUrl,
  wasmUrl
);

// Query is translated directly to HTTP 206 Partial Content range requests
const results = await worker.db.query(
  "SELECT context FROM contexts WHERE source_idx = 1010 AND target_idx = 2020"
);
console.log(results[0].context);
```

### 3. Reading CSR Traversal Binaries (Python)
The Compressed Sparse Row binaries contain contiguous arrays of indices for instantaneous link graph traversals without SQL execution overhead.

```python
import numpy as np
import gzip

# Read packed CSR binary
with gzip.open("adjacency_csr.bin.gz", "rb") as f:
    # 32-bit integer header: [N, E]
    header = np.frombuffer(f.read(8), dtype=np.uint32)
    N, E = header[0], header[1]
    
    # Offsets array (size N + 1): points to starting bounds of target connections
    offsets = np.frombuffer(f.read((N + 1) * 4), dtype=np.uint32)
    
    # Columns array (size E): stores the actual target indices
    columns = np.frombuffer(f.read(E * 4), dtype=np.uint32)

def get_neighbors(node_idx):
    if node_idx < 0 or node_idx >= N:
        return []
    start = offsets[node_idx]
    end = offsets[node_idx + 1]
    return columns[start:end]

print("Out-links for node ID 1010:", get_neighbors(1010))
```

---

## ⚙️ Layout & Data Pipeline

The dataset coordinates and binaries were generated via a multi-stage distributed pipeline:

1. **Wikipedia Extraction:** Standard SAX parsing of `enwiki-latest-pages-articles-multistream.xml.bz2` extracting valid hyperlinks.
2. **Pageviews Merging:** Joining nodes with monthly counts inside `pageviews.bz2` to compute node weight and relative radius sizing.
3. **Layout Generation:** Running a **GPU-accelerated ForceAtlas2 force-directed physics layout** using **NVIDIA RAPIDS cuGraph** over the complete 100M+ link edge-list. 
4. **Quantization:** Squeezing the double-precision float $(x, y)$ layout outputs into 16-bit unsigned integers mapped to a $384 \times 384$ coordinate grid system.
5. **CSR Indexing:** Compiling out-links and in-links arrays to pack structural graph traversal bytes.

---

## ⚖️ Citation & License
This dataset is compiled from the Wikimedia XML database dumps and pageviews files, which are distributed under the **Creative Commons Attribution-ShareAlike 4.0 International License (CC BY-SA 4.0)**. All code and scripts in the accompanying GitHub repository are licensed under the **MIT License**.
