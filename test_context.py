import requests
from bs4 import BeautifulSoup
import re
import json

title = "United States"
r = requests.get("https://en.wikipedia.org/w/api.php", params={
    "action": "parse", 
    "page": title, 
    "prop": "text", 
    "redirects": 1, 
    "format": "json"
}, headers={"User-Agent": "Bot/1.0"})
html = r.json()["parse"]["text"]["*"]
soup = BeautifulSoup(html, "html.parser")
contexts = {}
for p in soup.find_all(["p", "li"]):
    text = p.get_text()
    sentences = re.split(r'(?<=[.!?])\s+', text)
    for a in p.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/wiki/") and ":" not in href:
            link_title = requests.utils.unquote(href.replace("/wiki/", "").replace("_", " "))
            anchor_text = a.get_text()
            for s in sentences:
                if anchor_text in s:
                    clean_s = " ".join(s.split())
                    if 10 < len(clean_s) < 500:
                        contexts[link_title] = clean_s
                        break
print(f"Total contexts found: {len(contexts)}")
sample = {k: contexts[k] for k in list(contexts.keys())[:5]}
print(json.dumps(sample, indent=2))
