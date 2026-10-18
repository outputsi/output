"""Mirror person photos into app/static/people/ (run locally, commit the result).

Reads image URLs from the database:
    DATABASE_URL=... python tools/fetch_photos.py
Writes <person_id>.jpg (max 400px wide) and manifest.json {person_id: source_url}.
The site serves the local copy only while the DB image_url still equals the manifest URL.
"""
import io, json, os, time
import httpx
from PIL import Image
import sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static", "people")
UA = "OutputRank/1.0 (https://outputrank.com)"

def main():
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "manifest.json")
    man = json.load(open(path)) if os.path.exists(path) else {}
    for pid, url in db.people_images().items():
        if not url or (man.get(pid) == url and os.path.exists(os.path.join(OUT, pid + ".jpg"))):
            continue
        r = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=30)
        if r.status_code != 200:
            print("skip", pid, r.status_code); continue
        im = Image.open(io.BytesIO(r.content)).convert("RGB"); im.thumbnail((400, 800))
        im.save(os.path.join(OUT, pid + ".jpg"), "JPEG", quality=82, optimize=True)
        man[pid] = url; time.sleep(1)
    json.dump(man, open(path, "w"), indent=1, sort_keys=True)

if __name__ == "__main__":
    main()
