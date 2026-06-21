# Project Progress: Wikipedia Graph Visualizer

## Phase: Experimental Branch Enhancements (June 2026)

### 🚀 Key Improvements Implemented
- **High-Quality Link Contexts:**
    - Refactored scraper to capture the exact sentence where a link appears using `BeautifulSoup`.
    - Implemented **Body-First Discovery**: The scraper now ignores `References`, `External Links`, and `Notes` sections, ensuring connections only represent meaningful relationships in the article text.
    - **Multi-Pass Context Search**:
        1.  Extracts sentence containing the actual `<a>` tag.
        2.  Fallback: Searches body paragraphs for the literal title of the linked node if the link sentence is too short (< 10 words).
        3.  Last Resort: Checks tables and lists within the body.
- **Frontend UI/UX Overhaul:**
    - **Connections Sidebar**: Replaced "Network Neighbors" with a more descriptive "Connections" list.
    - **Expandable Snippets**: Connection contexts are now interactive—click to expand truncated sentences.
    - **Smart Navigation**: Separate click handlers for node names (navigation) and context text (expansion).
    - **Interactive Tooltips**: Added `?` help icons across the settings panel to explain physics and density controls.
- **System Resilience:**
    - Added `safe_request` wrapper with exponential backoff for handling `429 Too Many Requests`.
    - Persistent "Resume" capability: Crawler tracks progress in `wiki_cache.db` and can pick up exactly where it left off.
    - Circuit Breaker: Automatically stops execution after 5 consecutive failed articles to prevent IP bans.

- **8M WebGL Visualizer Overhaul (`8m_optimized.html`):**
    - **Scale Render**: Flipped render limit from 1M to 8M nodes, validated WebGL stability under Firefox/Chrome constraints.
    - **Worker-Driven Coordinates**: Integrated zero-copy Web Worker to asynchronously fetch and unpack `coordinates.bin` (64MB) directly into binary Float32Arrays.
    - **Memory Safety**: Designed a main-thread `Proxy` array to feed node coordinates on-the-fly to Cosmograph, avoiding memory allocation overhead of 8,000,000 standard JS objects.
    - **SQLite HTTP VFS**: Integrated `sqlite-wasm-http` to stream node details and link contexts from the 35GB `wiki_simulation.db` using HTTP Range Requests.
    - **Batch-Oriented Pathfinder**: Implemented a bidirectional BFS that batches node expansions into a single SQL `IN(...)` query per level, minimizing network latency over VFS range requests.
- **XML Dump Parser (`scraper/xml_parser.py`):**
    - Developed a memory-safe, streaming XML parser (`ElementTree.iterparse`) to process Wikipedia XML database dumps.
    - Implemented wikitext context extraction to find full-sentence contexts directly from Wikitext markup, bypassing the slow MediaWiki parse API.
    - Integrated support for pageviews dumps to match article pageviews.

### 🧪 Current Test Environment (`/test_scrape`)
An isolated environment has been set up to validate the new logic before moving back to production:
- `8m_optimized.html`: Full-scale WebGL visualizer with VFS range requests and 8M coordinates.
- `scraper.py`: Hardened version with 1.0s delay and body-only extraction.
- `xml_parser.py`: Fast XML Dump Parser streaming to `wiki_cache.db`.
- `compiler.py`: Generates local `wiki_graph.db`.
- `run_test.sh`: Automated pipeline (Scrape 100 → Compile → Build Visualizer).

### 🛠️ Technical Standards Established
- **Context Preservation**: The `links` table in the final SQLite database now includes a `context` column.
- **Noise Reduction**: Only links found in the descriptive text are included in the graph; citation-only links are discarded.
- **Template Consistency**: All UI and CSS improvements are synced to `generator/template.html` for future builds.

### 🏗️ Layout Architecture Pivot (In Progress)
- **Abandoning Custom C Simulator:** The custom C layout simulator was deemed too risky for the 8M node scale.
- **CPU `graph-tool` Constraints:** Attempted to use `graph-tool` (SFDP multilevel layout) locally on an M5 Mac (24GB RAM). Hit severe memory swap issues when processing 6.9M nodes and 100M edges. Attempted a "Skeleton & Skin" strategy (simulating only nodes with high degrees and orbiting the rest), but visual results remained too clumped or suffered from mathematical artifacts (e.g. "laser beam" orphan nodes).
- **Pivoting to RAPIDS cuGraph (GPU):** Shifted layout computation to a cloud GPU instance (RunPod RTX 3090/4090) to utilize NVIDIA's `cugraph` and `cudf`.
- **Cohesive Cosmic Web Aesthetic:** We aligned on a purely connection-driven layout that groups category continents while maintaining interdisciplinary bridges. High-degree hubs repel each other strongly via degree-scaled ForceAtlas2 physics, but draw their direct neighbors in to form moderate local clusters (prevented from clumping by log-scaled pageview boundaries and `prevent_overlapping=True`).
- **Multi-Stage Layout Engine (`compile_galaxy_multistage.py`):** Implemented a three-phase Progressive Layered Simulation layout engine to solve topological flattening, context destruction, and clumping problems of simple barycentric approximation:
    1. **Phase 1: Continental Crust ($k \ge 3$):** Extract nodes with $k \ge 3$ (~1.5M) and run a deep ForceAtlas2 layout (600 iterations, gravity `0.2`, scaling `80.0`) to establish clear global thematic continents.
    2. **Phase 2: Ingestion & Core-Pinning Simulation:** 
        - **Gateway Label Propagation:** Map every $k < 3$ peripheral node to its nearest core gateway node in terms of graph distance using a GPU-parallel BFS propagation loop.
        - **Vector-Offset Initialization:** Initialize peripheral nodes near their gateways with deterministic radial offsets based on node IDs (preventing central coordinate collapse).
        - **Core-Pinning Simulation:** Run 250 iterations of ForceAtlas2 on the full graph, resetting core coordinates to their Phase 1 positions at the end of each sub-step to act as stable physical gravity anchors.
    3. **Phase 3: The Global Polish & Fine Settle:** Unpin the core and relax the full graph (80 iterations without overlap prevention for speed, plus 40 iterations with `prevent_overlapping=True` to resolve fine-grained overlap details).
    4. **Orphan Cosmic Dust:** Scatter degree-0 orphans uniformly across a wide bounding box as background stars.

### 📅 Next Steps
1. Upload `edges_weighted.csv.gz` and `metadata.csv` to RunPod.
2. Execute the multi-stage compiler on the RunPod GPU instance: `python compile_galaxy_multistage.py`.
3. Verify if the resulting `coordinates_rapids.bin` resolves the density, clumping, and artifact issues under the cohesive cosmic web parameters.
4. Verify VFS Range Request speed and WebGL rendering of 8M nodes in `test_scrape/8m_optimized.html` locally using `log_server.py`.
5. Import a real Wikipedia XML dump and compile it using the new `scraper/xml_parser.py` pipeline.
6. Deploy the 35GB database to Hugging Face Datasets and host the visualizer on GitHub Pages.

