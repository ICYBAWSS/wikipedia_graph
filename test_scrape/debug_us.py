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
content = soup.find(class_="mw-parser-output") or soup

print(f"Content found: {content is not None}")
p_count = 0
for p in content.find_all(["p", "li"]):
    p_count += 1
    p_text = p.get_text()
    if "Instagram" in p_text:
        print(f"Found Instagram in p/li #{p_count}")
        sentences = re.split(r'(?<=[.!?])\s+', p_text)
        print(f"Sentences: {len(sentences)}")
        for a in p.find_all("a", href=True):
            print(f"  Link: {a['href']} | Anchor: '{a.get_text()}'")
            if "Instagram" in a['href']:
                 anchor_text = a.get_text().strip()
                 for s in sentences:
                     if anchor_text in s:
                         print(f"    MATCHED SENTENCE: {s}")
        break
