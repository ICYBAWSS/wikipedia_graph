#!/bin/bash
# Script to upload remaining huge files to Hugging Face dataset
set -e

echo "Uploading test_scrape/wiki_graph.db as test_scrape/wiki_simulation.db..."
hf upload icybawss/wikipedia-graph-data test_scrape/wiki_graph.db test_scrape/wiki_simulation.db --repo-type dataset

echo "Uploading test_scrape/wiki_graph.db..."
hf upload icybawss/wikipedia-graph-data test_scrape/wiki_graph.db test_scrape/wiki_graph.db --repo-type dataset

echo "Uploading test_scrape/wiki_cache.db..."
hf upload icybawss/wikipedia-graph-data test_scrape/wiki_cache.db test_scrape/wiki_cache.db --repo-type dataset

echo "Uploading test_scrape/enwiki-latest-pages-articles-multistream.xml.bz2..."
hf upload icybawss/wikipedia-graph-data test_scrape/enwiki-latest-pages-articles-multistream.xml.bz2 test_scrape/enwiki-latest-pages-articles-multistream.xml.bz2 --repo-type dataset

echo "Uploading test_scrape/pageviews.bz2..."
hf upload icybawss/wikipedia-graph-data test_scrape/pageviews.bz2 test_scrape/pageviews.bz2 --repo-type dataset

echo "All uploads completed successfully!"
