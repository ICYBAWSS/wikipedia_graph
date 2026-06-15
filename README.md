> currenly only 1000 articles are in, doing heavy optimization work before scraping and adding all 7,195,600.

## My computer did all the hard scraping and proccesing for you! Just use the github pages link https://icybawss.github.io/wikipedia_graph/ no need to do anything below unless you would like to remake it or edit stuff.

# 1. Project Documentation (README.md)
## Wikipedia Graph Visualization Pipeline

This project aims to create an interactive web-based visualization (an Obsidian-like node graph) of Wikipedia articles and their connections.

### Prerequisites
You must have Python and the following packages installed:
`pip install requests beautifulsoup4`

### Project Structure
- `/wikipedia_graph_project/`
    - `scraper/`: Contains the core data-fetching logic.
    - `generator/`: Contains the visualization logic (D3.js).
    - `results/`: Final JSON data and HTML artifacts.

### Execution Steps
1. **Data Scraper:** Run the scraping scripts to gather all the raw data.
2. **Data Compiler:** Run the compilation script to transform the raw data into the D3-ready JSON.
3. **Visualization Builder:** Run the final script to embed the data into the D3.js HTML/CSS visualization.

### IMPORTANT NOTE
The script provided previously was a conceptual blueprint. Due to the scope of 'all of Wikipedia', the ingestion phase (Phase 3) is highly complex. Start with small batches (e.g., processing 100 pages) and verify the extraction logic before attempting full-scale runs.
