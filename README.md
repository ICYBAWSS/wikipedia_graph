# https://icybawss.github.io/wikipedia_graph/
- If its laggy try to reduce node budget in settings menu.
- IOS via safari does not work. Other browsers and android not tested.
# Wikipedia Graph Visualizer & Pathfinder

An interactive, high-performance, serverless client-side visualization and route-finding engine for the entire English Wikipedia link graph. This application visualizes **~6.9 million articles** (nodes) and **~100 million links** (edges) as a cohesive "cosmic web" layout, allowing users to find paths between arbitrary pages instantly in the browser.

---

## Architecture Overview

The system is designed to run entirely in the browser with **zero active server-side compute**. It relies on two main components:
1. **WebGL Rendering Engine:** Powered by **Deck.gl** with custom viewport-culling and Level-of-Detail (LOD) rendering to achieve smooth 60fps interaction on millions of nodes.
2. **SQLite Virtual File System (VFS):** Uses `sql-httpvfs` to run SQL queries inside a browser Web Worker directly against a static 25 GB database hosted on Hugging Face using standard HTTP Range Requests.

```
                           +----------------------------+
                           |   Browser / client-side    |
                           +-------------+--------------+
                                         |
            +----------------------------+----------------------------+
            |                                                         |
+-----------v-----------+                                 +-----------v-----------+
|  WebGL Render Canvas  |                                 |   In-Memory Engines   |
|   (Deck.gl Layered)   |                                 |   (Search/CSR/Titles) |
+-----------+-----------+                                 +-----------+-----------+
            |                                                         |
+-----------v-----------+     HTTP range queries (64KB chunks)        |
|  sqlite.worker.js     |=============[ HTTP Range Requests ]========>|  Hugging Face / CDN:  |
|  (sql-httpvfs + WASM) |                                             |  - wiki_simulation.db |
+-----------------------+                                             |  - adjacency_csr.bin  |
                                                                      +-----------------------+
```

---

## Component Deep Dive

### 1. High-Performance Front-End (`engine.js`)
The client-side engine in [`engine.js`](file:///Users/rayhan/wikipedia_graph_project/engine.js) drives the application. Key optimizations include:
- **Viewport Culling (Grid Binning):** The 2D layout space is divided into a $384 \times 384$ grid. Nodes are binned into these cells. The engine dynamically calculates which cells intersect the camera view frustum and maps a strict draw budget (default: 90,000 nodes) across visible cells.
- **Priority Query Scheduler:** A scheduler prioritizes VFS queries based on user action:
  - `PRIORITY_CLICK` (3) / `PRIORITY_SEARCH` (2) take precedence.
  - `PRIORITY_HOVER` (1) / `PRIORITY_CULL` (0) are automatically evicted or aborted if a newer user interaction begins.
- **Interactive Connections Preview:** When a node is selected, [`engine.js`](file:///Users/rayhan/wikipedia_graph_project/engine.js) immediately renders its immediate neighbors (up to 40) using the local CSR index, then pulls wikitext snippet contexts asynchronously in the background.

### 2. Pathfinding Engine
The visualizer offers multiple pathfinders running directly over in-memory CSR arrays:
- **Bidirectional BFS:** The primary pathfinder. It searches forward from the source node (walking out-links via `adjacency_csr.bin`) and backward from the destination (walking in-links via `adjacency_csr_rev.bin`). It expands the smaller frontier at each step to maintain a complexity of $O(b^{d/2})$.
- **Dijkstra (Weighted Hub Penalization):** To avoid routing paths through generic hubs (e.g., "United States", "Wikipedia"), Dijkstra uses a logarithmic degree-penalized edge cost:
  $$\text{cost}(a, b) = 1 + 0.5 \times \ln(1 + \text{deg}(b))$$
- **A\* / Greedy Best-First:** Employs straight-line layout coordinate distance to target as the guiding heuristic.
- **Random Walk:** A baseline stumbler capped at 20,000 steps.

### 3. Binary Asset Serialization (`build_web_assets.py`)
To avoid loading hundreds of megabytes of raw floats on launch, files are packed and quantized into high-density binary arrays:
* **`viewer_v2.bin.gz` (Layout Data):**
  - **Coordinates:** Floating-point coordinates ($x, y$) are scaled and quantized to `uint16` over a shared grid. The precision loss is $\approx 0.044$ units, which translates to a negligible $0.06$ pixels at full-graph zoom.
  - **Degrees:** Unique degrees are stored in a float32 palette, and each node stores a `uint16` index pointing into this palette.
  - **Categories:** Stored as raw `uint8` indexes representing the 7 semantic categories.
* **`edgeTgt_v2.bin.gz` (Ambient Filament Targets):** Stores each node's single strongest neighbor coordinate. Active presence is packed into a 1-bit boolean mask (1 bit per node), padded to maintain alignment.
* **`titles_v2.bin.gz` (Local Autocomplete Index):** Stores a concatenated UTF-8 byte array of all titles. Offsets are rebuilt at load time by prefix-summing an array of `uint8` title lengths.

---

## Replication & Setup Guide

### Prerequisites
- Python 3.10+
- Node.js (for local server or tooling)
- SQLite3
- [Graph-tool](https://graph-tool.skewed.de/) (Required only for generating static high-res renders)

### 1. Running the Local Development Server
To query sqlite files correctly using range requests, the server must support CORS, Range headers (`Accept-Ranges: bytes`), and Partial Content (`206`). A preconfigured development server is provided in [`log_server.py`](file:///Users/rayhan/wikipedia_graph_project/log_server.py):

```bash
python log_server.py
```
This serves the project directory on port `8000` and outputs browser console diagnostics directly to `console_logs.txt`.

### 2. Scraping & Database Rebuilding
If you want to pull data directly from Wikipedia:
* **API Scraper:** To incrementally scrape seeds:
  ```bash
  python scraper/scraper.py
  ```
* **XML Dump Parser:** To parse a raw Wikimedia XML dump (`pages-articles.xml.bz2`):
  ```bash
  python scraper/xml_parser.py --xml /path/to/dump.xml.bz2
  ```

### 3. Rebuilding Visualizer Binaries
To compile raw coordinate data and SQLite links into quantized v2 assets:
1. Ensure `wiki_simulation_full.db` and the coordinate layouts (`coordinates_rapids.bin` or `viewer_full.bin`) are in the root directory.
2. Run the repack layout utility:
   ```bash
   python runpod/rebuild_full_layout.py --db wiki_simulation_full.db --coords coordinates_rapids.bin
   ```
3. Quantize and compress the outputs into v2 assets:
   ```bash
   python build_web_assets.py
   ```
This generates `viewer_v2.bin.gz`, `edgeTgt_v2.bin.gz`, and `titles_v2.bin.gz` in the root folder, ready for deployment.

### 4. Running Benchmarks & Tests
Functional test assertions (VFS eviction, pathfinders, autocomplete, UI rendering) are implemented in [`tests/harness.js`](file:///Users/rayhan/wikipedia_graph_project/tests/harness.js). You can run them inside the browser console:

```javascript
// Load the test script
await fetch('/tests/harness.js').then(r => r.text()).then(eval);

// Run functional test suite
await WGTest.run();

// Run latency benchmarks
await WGTest.bench();
```

---

## Contributor & Maintainer Notes

### schema of `wiki_simulation.db` / `wiki_cache.db`
- **`nodes`** table:
  - `rowid` (INTEGER, 1-indexed, maps directly to the coordinate and CSR arrays at offset `rowid - 1`).
  - `id` (TEXT, primary key representing the article title).
  - `category` (TEXT, semantic category mapping).
  - `inDegree` (INTEGER) / `outDegree` (INTEGER).
  - `snippet` (TEXT, parsed wikitext excerpt).
- **`links`** table:
  - `source_idx` (INTEGER, index of the source article matching node `rowid - 1`).
  - `target_idx` (INTEGER, index of the target article matching node `rowid - 1`).
  - `source` / `target` (TEXT, titles of source and target).
  - `context` (TEXT, sentence snippet showing where the wikitext link occurred).

> [!WARNING]
> **Never query the links table using both `source_idx` and `target_idx` in a single query.** Because the SQLite database is queried over HTTP range requests, SQLite's planner will pick one index (typically target) and perform hundreds of random lookups back into the table for the other column. Under `sql-httpvfs`, this triggers hundreds of separate 64KB range queries, causing the browser thread to hang. Always anchor query predicates on `source_idx` (outgoing links) or `target_idx` (incoming links) independently, then filter or merge results client-side.

### Hosting Large Binaries
Because files like `adjacency_csr.bin.gz` and `adjacency_csr_rev.bin.gz` are ~250MB each, they exceed GitHub's single-file size recommendations. They are hosted on Hugging Face at:
`https://huggingface.co/datasets/icybawss/wikipedia-graph-data/resolve/main/`

If these files are updated:
1. Upload them to the Hugging Face dataset repository.
2. Update the `HF_ASSET_BASE` URL in [`engine.js`](file:///Users/rayhan/wikipedia_graph_project/engine.js) if necessary.
3. Keep `V` cache-buster versions in sync.
