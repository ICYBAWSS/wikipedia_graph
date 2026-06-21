import os
import time
import subprocess
from datetime import datetime

LOG_FILE = "pipeline.log"
STATUS_FILE = "status_report.log"
DB_CACHE = "wiki_cache.db"
DB_FINAL = "wiki_simulation.db"

def get_size(path):
    if os.path.exists(path):
        return f"{os.path.getsize(path) / (1024**3):.2f} GB"
    return "N/A"

def get_last_line(path):
    if not os.path.exists(path): return "Log not found."
    try:
        line = subprocess.check_output(['tail', '-n', '1', path]).decode().strip()
        return line
    except: return "Error reading log."

print("Monitoring started. Logging to status_report.log every hour.")

while True:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    last_log = get_last_line(LOG_FILE)
    cache_size = get_size(DB_CACHE)
    final_size = get_size(DB_FINAL)
    
    report = f"[{now}] Status: {last_log} | Cache: {cache_size} | Final: {final_size}\n"
    
    with open(STATUS_FILE, "a") as f:
        f.write(report)
        
    time.sleep(3600) # Wait 1 hour
