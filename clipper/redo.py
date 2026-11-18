"""Makes an uploaded Short again from the stream recording (e.g. the webcam was missing) and replaces it.

Requests are in redo.yaml. The moment is cut from Kick's recording of the stream on the runner (nothing is
downloaded to a PC). The new Short is uploaded first; only then is the old one deleted."""
import argparse
import subprocess
from pathlib import Path

import requests
import yaml

from . import cam, render, youtube
from .kick import H
from .llm import LLM
from .watch import describe, log

ROOT = Path(__file__).resolve().parent.parent


def recording(slug: str, stream_id: str) -> dict:
    r = requests.get(f"https://kick.com/api/v2/channels/{slug}/videos", headers=H, timeout=30)
    r.raise_for_status()
    for v in r.json():
        if str(v.get("id")) == str(stream_id):
            return v
    raise RuntimeError(f"{slug}: {stream_id} yayınının kaydı bulunamadı")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    reqs = yaml.safe_load((ROOT / "redo.yaml").read_text(encoding="utf-8")) or []
    channel = cfg["youtube_channel"]
    llm = LLM(cfg)
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    uploads = [] if args.dry_run else youtube.recent_uploads(channel)
    for q in reqs:
        who, old, sec = q["streamer"], q["replace"], int(q["sec"])
        if any(f"redo:{old}" in u["description"] for u in uploads):
            log(f"{old}: zaten yeniden yapılmış, atlandı.")
            continue
        v = recording(who, q["stream"])
        stamp = f"{who}_redo_{old}"
        raw = out / f"{stamp}_raw.mp4"
        a, b = sec - q.get("before_s", 38), sec + q.get("after_s", 15)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(max(a, 0)), "-i", v["source"], "-t", str(b - a),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", str(raw)], check=True)
        try:
            box = cam.find(llm, cam.frames([raw], per_video=3))
            log(f"kamera: {cam.where(box)} {box or ''}")
        except Exception as e:
            log(f"kamera kontrolü yapılamadı: {type(e).__name__}: {e}")
            box = None
        box = box or cfg.get("cams", {}).get(who)
        words = render.transcribe(raw)
        seen = render.look(raw)
        try:
            meta = describe(llm, words, who, v.get("session_title", ""), q.get("category", ""), seen["frames"])
        except RuntimeError as e:
            log(f"kanca yazılamadı: {e}")
            meta = {}
        final = render.render(raw, out / f"{stamp}.mp4", words, meta.get("hook", ""), who, box)
        raw.unlink(missing_ok=True)
        title = q.get("title") or meta.get("title") or f"{who} yayınından #shorts"
        desc = (f"{who} canlı yayınından bir an.\nYayıncı: https://kick.com/{who}\n\n#{who} #kick #shorts\n\n"
                f"moment:{q['stream']}:{sec}\nredo:{old}")
        if args.dry_run:
            log(f"[deneme] hazır: {final.name} | {title} | kanca: {meta.get('hook', '')}")
            continue
        vid = youtube.upload(channel, final, title, desc, meta.get("tags", []), 5)
        log(f"yeni hali yüklendi: https://youtu.be/{vid} | {title}")
        youtube.delete(channel, old)
        log(f"eski hali silindi: {old}")


if __name__ == "__main__":
    main()
