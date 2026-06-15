import os
import requests

LIBS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "generator", "libs"))
os.makedirs(LIBS_DIR, exist_ok=True)

LIBS = {
    "pako.min.js": "https://cdnjs.cloudflare.com/ajax/libs/pako/2.1.0/pako.min.js",
    "sql-wasm.js": "https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.8.0/sql-wasm.js",
    "sql-wasm.wasm": "https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.8.0/sql-wasm.wasm"
}

def download():
    for name, url in LIBS.items():
        path = os.path.join(LIBS_DIR, name)
        if os.path.exists(path):
            print(f"{name} already exists. Skipping.")
            continue
        print(f"Downloading {name} from {url}...")
        try:
            r = requests.get(url, stream=True, timeout=30)
            if r.status_code == 200:
                with open(path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024):
                        f.write(chunk)
                print(f"Successfully downloaded {name}.")
            else:
                print(f"Failed to download {name}: HTTP {r.status_code}")
        except Exception as e:
            print(f"Exception downloading {name}: {e}")

if __name__ == "__main__":
    download()
