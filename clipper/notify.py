"""Phone alert for a very good clip, so it can be posted to TikTok by hand.

TikTok keeps refusing API uploads from the unaudited app, so instead the owner gets a push notification
(ntfy app, free, no account) with the clip attached as a small mp4: tap, save, post on TikTok.

The topic name is derived from the secret TIKTOK_TOKEN_KEY, so it is not written anywhere public:
  python -m clipper.notify topic     -> prints the topic to subscribe to in the ntfy app
  python -m clipper.notify test      -> sends a test message
"""
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent


def topic() -> str | None:
    key = os.environ.get("TIKTOK_TOKEN_KEY")
    if not key:
        f = ROOT / "credentials" / "tiktok_token_key.txt"
        key = f.read_text(encoding="utf-8").strip() if f.exists() else None
    return "efsane-kesit-" + hashlib.sha256(key.encode()).hexdigest()[:20] if key else None


def _count(add: bool = False) -> int:
    """Alerts sent today, kept with the encrypted TikTok token (shared by every job). 0 when unknown."""
    try:
        from .tiktok import _load, _save_raw, _today
        tok = _load()
        n = int(tok.get("notified", {}).get(_today(), 0))
        if add:
            tok["notified"] = {_today(): n + 1}
            _save_raw(tok)
        return n
    except Exception:
        return 0


RELEASE = "telefon"  # one public release that holds the latest clips for the phone


def host(video: Path) -> str | None:
    """Puts the clip on the repo's 'telefon' release and returns its download link (None when not on GitHub).
    The clips are public YouTube Shorts anyway; only the newest 20 are kept."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    api = f"https://api.github.com/repos/{repo}"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    r = requests.get(f"{api}/releases/tags/{RELEASE}", headers=h, timeout=30)
    if r.status_code == 404:
        r = requests.post(f"{api}/releases", headers=h, timeout=30, json={
            "tag_name": RELEASE, "name": "Telefon kesitleri",
            "body": "TikTok'a elle yüklemek için kesitler (bildirimle gelir). En yeni 20 tanesi tutulur."})
    r.raise_for_status()
    rel = r.json()
    assets = sorted(rel.get("assets", []), key=lambda a: a["created_at"])
    for a in assets[:-19]:
        requests.delete(f"{api}/releases/assets/{a['id']}", headers=h, timeout=30)
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in video.stem)[:40]
    name = f"kesit-{time.strftime('%m%d-%H%M%S')}-{stem}.mp4"
    up = rel["upload_url"].split("{")[0]
    r = requests.post(up, params={"name": name}, data=video.read_bytes(), timeout=300,
                      headers={**h, "Content-Type": "video/mp4"})
    r.raise_for_status()
    return r.json()["browser_download_url"]


def small_copy(video: Path) -> Path:
    """A copy under MAX_MB for the phone (60 s at ~1.6 Mbit/s)."""
    out = video.with_name(video.stem + "_tel.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-c:v", "libx264", "-preset", "veryfast",
                    "-b:v", "1500k", "-maxrate", "1700k", "-bufsize", "3000k", "-c:a", "aac", "-b:a", "96k",
                    "-movflags", "+faststart", str(out)], check=True)
    return out


def alert(cfg: dict, video: Path, title: str, link: str, score: float, floor_key: str, log) -> None:
    """Sends the alert when score >= notify[floor_key]. Never breaks the run."""
    n = cfg.get("notify", {})
    t = topic()
    if not n.get("enabled") or not t or score < n.get(floor_key, 99):
        return
    top = score >= n.get("top_" + floor_key, 99)  # a standout always comes, even past the daily count
    sent = _count()
    if not top and sent >= n.get("per_day", 99):
        log(f"telefona bildirilmedi: bugünkü {sent} bildirim doldu (puan {score:.1f})")
        return
    try:
        q = {"title": f"{'🔥 Çok iyi! ' if top else ''}TikTok'a yükle ({score:.1f})", "click": link, "tags": "fire",
             "message": f"{title} | {link} | Dosyayı kaydet, TikTok'ta paylaş."}
        url = host(video)  # ntfy.sh takes only ~2 MB attachments: the mp4 is linked from a GitHub release
        if url:  # tapping the alert opens the file; the YouTube link stays in the text
            # a different name per clip: the same name twice is saved as "kesit.mp4 (1)", which TikTok rejects
            q.update({"attach": url, "filename": url.rsplit("/", 1)[-1], "click": url,
                      "message": f"{title} | Dosya: {url} | YouTube: {link}"})
        r = requests.post(f"https://ntfy.sh/{t}", params=q, timeout=30)
        log(f"telefona bildirildi (puan {score:.1f}): HTTP {r.status_code}")
        if r.ok:
            _count(add=True)
    except Exception as e:
        log(f"telefona bildirilemedi: {type(e).__name__}: {e}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "topic":
        print(topic())
    elif cmd == "test":
        r = requests.post(f"https://ntfy.sh/{topic()}", timeout=30, params={
            "title": "Efsane Yayın Anları", "tags": "white_check_mark",
            "message": "Kesit bildirimleri çalışıyor. Çok iyi bir kesit çıkınca burada dosyasıyla gelecek."})
        print(r.status_code)
    else:
        print(__doc__)
