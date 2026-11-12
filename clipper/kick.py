"""Public Kick endpoints: is a channel live, its stream URL and category."""
import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
     "Accept": "application/json"}


def channel(slug: str) -> dict:
    r = requests.get(f"https://kick.com/api/v2/channels/{slug}", headers=H, timeout=30)
    r.raise_for_status()
    return r.json()


def live_info(slug: str) -> dict | None:
    """None when offline, else {id, title, category, playback_url, started}."""
    d = channel(slug)
    ls = d.get("livestream")
    if not ls or not ls.get("is_live"):
        return None
    cats = ls.get("categories") or [{}]
    return {"id": str(ls["id"]), "title": ls.get("session_title", ""), "category": cats[0].get("name", ""),
            "playback_url": d["playback_url"], "started": ls.get("start_time") or ls.get("created_at")}


def best_variant(master_url: str) -> str:
    """The 1080p (else highest) media playlist of a live master playlist."""
    r = requests.get(master_url, headers=H, timeout=30)
    r.raise_for_status()
    lines = r.text.splitlines()
    best, best_h = master_url, 0
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and "RESOLUTION=" in line and i + 1 < len(lines):
            h = int(line.split("RESOLUTION=")[1].split(",")[0].split("x")[1])
            if best_h < h <= 1080:
                uri = lines[i + 1].strip()
                best = uri if uri.startswith("http") else master_url.rsplit("/", 1)[0] + "/" + uri
                best_h = h
    return best
