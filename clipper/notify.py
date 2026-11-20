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
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
MAX_MB = 14  # ntfy.sh attachment limit is 15 MB


def topic() -> str | None:
    key = os.environ.get("TIKTOK_TOKEN_KEY")
    if not key:
        f = ROOT / "credentials" / "tiktok_token_key.txt"
        key = f.read_text(encoding="utf-8").strip() if f.exists() else None
    return "efsane-kesit-" + hashlib.sha256(key.encode()).hexdigest()[:20] if key else None


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
    try:
        small = small_copy(video)
        if small.stat().st_size > MAX_MB * 2**20:
            small = None
        q = {"title": f"TikTok'a yükle ({score:.1f})", "click": link, "tags": "fire",
             "message": f"{title} | {link} | Dosyayı kaydet, TikTok'ta paylaş."}
        url = f"https://ntfy.sh/{t}"
        if small:
            r = requests.put(url, params={**q, "filename": "kesit.mp4"}, data=small.read_bytes(), timeout=120)
            small.unlink(missing_ok=True)
        else:
            r = requests.post(url, params=q, timeout=30)
        log(f"telefona bildirildi (puan {score:.1f}): HTTP {r.status_code}")
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
