#!/bin/bash

echo "--- Phase 1: Scraping 100 articles ---"
python3 scraper.py 100

echo ""
echo "--- Phase 2: Compiling Graph ---"
python3 compiler.py

echo ""
echo "--- Phase 3: Building Visualizer ---"
python3 builder.py

echo ""
echo "Done! You can now open test_scrape/index.html in your browser."
