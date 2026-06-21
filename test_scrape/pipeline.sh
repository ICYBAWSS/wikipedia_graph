#!/bin/bash
# Wikipedia Graph Visualizer - Full Data Pipeline
set -e

# 0. Cleanup failed previous attempts
rm -f wiki_cache.db wiki_simulation.db wiki_graph.db pageviews.bz2

# 1. Download Pageviews (Fast)
echo "Downloading May 2026 pageviews..."
curl -fL -o pageviews.bz2 https://dumps.wikimedia.org/other/pageview_complete/monthly/2026/2026-05/pageviews-202605-user.bz2

# 2. Parse XML Dump to Cache
if [ -f "enwiki-latest-pages-articles-multistream.xml.bz2" ]; then
    echo "Starting XML Ingestion..."
    python3 xml_parser.py --file enwiki-latest-pages-articles-multistream.xml.bz2 --views pageviews.bz2
else
    echo "Error: enwiki-latest-pages-articles-multistream.xml.bz2 not found!"
    exit 1
fi

# 3. Compile Cache to Graph DB
echo "Compiling Graph Database..."
python3 compiler.py
mv wiki_graph.db wiki_simulation.db

# 4. Run Coordinate Layout (C-powered)
echo "Generating Coordinates (80 iterations)..."
gcc -O3 -march=native layout_simulator.c -lsqlite3 -o layout_simulator
./layout_simulator 1.0 0.5 coordinates.bin 1

echo "Pipeline Complete! Open 8m_optimized.html to view."
