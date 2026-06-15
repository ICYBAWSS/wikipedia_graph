import os
import base64
import gzip

TEMPLATE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "template.html"))
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "results", "wiki_graph.db"))
LIBS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "libs"))
OUTPUT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "index.html"))

def build_visualization():
    """Compiles the SQLite database and base64-encoded WASM libraries into a self-contained HTML file."""
    if not os.path.exists(DB_PATH):
        print(f"Compiled database not found at {DB_PATH}. Please run the compiler first.")
        return
        
    if not os.path.exists(TEMPLATE_PATH):
        print(f"Template not found at {TEMPLATE_PATH}.")
        return

    # Check dependencies exist
    pako_path = os.path.join(LIBS_DIR, "pako.min.js")
    sql_js_path = os.path.join(LIBS_DIR, "sql-wasm.js")
    sql_wasm_path = os.path.join(LIBS_DIR, "sql-wasm.wasm")
    
    for dep in [pako_path, sql_js_path, sql_wasm_path]:
        if not os.path.exists(dep):
            print(f"Required dependency not found: {dep}. Please run scraper/download_libs.py first.")
            return

    # 1. Read and compress the SQLite database
    print("Compressing SQLite database...")
    with open(DB_PATH, "rb") as f:
        db_bytes = f.read()
    compressed_db = gzip.compress(db_bytes)
    db_base64 = base64.b64encode(compressed_db).decode("utf-8")
    print(f"Database compressed: {len(db_bytes) / 1024 / 1024:.2f} MB -> {len(compressed_db) / 1024 / 1024:.2f} MB (Base64 length: {len(db_base64) / 1024 / 1024:.2f} MB)")

    # 2. Read dependencies
    print("Reading dependency libraries...")
    with open(pako_path, "r", encoding="utf-8") as f:
        pako_js = f.read()
        
    with open(sql_js_path, "r", encoding="utf-8") as f:
        sql_js = f.read()
        
    with open(sql_wasm_path, "rb") as f:
        wasm_bytes = f.read()
    sql_wasm_base64 = base64.b64encode(wasm_bytes).decode("utf-8")

    # 3. Read template and inject dependencies/database
    print("Compiling self-contained visualizer page...")
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template_content = f.read()

    replacements = {
        "/* PAKO_JS_PLACEHOLDER */": pako_js,
        "/* SQL_JS_PLACEHOLDER */": sql_js,
        "/* SQL_WASM_BASE64_PLACEHOLDER */": sql_wasm_base64,
        "/* COMPRESSED_SQLITE_DB_PLACEHOLDER */": db_base64
    }

    final_content = template_content
    for placeholder, code in replacements.items():
        if placeholder not in template_content:
            print(f"Warning: Could not find placeholder '{placeholder}' in template.")
        final_content = final_content.replace(placeholder, code)

    # Write out self-contained HTML page
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(final_content)
        
    print(f"Visualization created successfully! Saved to {OUTPUT_PATH}")

if __name__ == "__main__":
    build_visualization()
