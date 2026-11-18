"""Makes uploaded Shorts again (e.g. the webcam was missing) and replaces them.

Requests are in redo.yaml: one Short (replace: <id>) or all Shorts of a streamer on a day (streamer + date).
Stream and second come from the old Short's description ("moment:" line; "clip:" for a viewer clip).
The moment is cut from Kick's recording on the runner (nothing is downloaded to a PC).
The new Short is uploaded first; only then is the old one deleted."""
import argparse
import re
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


def jobs(reqs: list[dict], uploads: list[dict]) -> list[dict]:
    out = []
    for q in reqs:
        if "replace" in q and "sec" in q:  # fully written request
            out.append(q)
            continue
        for u in uploads:
            d = u["description"]
            if q.get("replace"):
                if u["id"] != q["replace"]:
                    continue
            elif "redo:" in d or f"kick.com/{q['streamer']}" not in d:
                continue
            if q.get("date") and not u["published"].startswith(str(q["date"])):
                continue
            m = re.search(r"moment:(\d+):(\d+)", d)
            c = re.search(r"^clip:(\S+)", d, re.M)
            if not m and not c:
                log(f"{u['id']}: açıklamada an bilgisi yok, atlandı.")
                continue
            out.append({"streamer": q["streamer"], "replace": u["id"], "title": u["title"],
                        "stream": m.group(1) if m else str(q.get("stream", "")),
                        "sec": int(m.group(2)) if m else 0, "clip": c.group(1) if c else None})
    return out


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
    uploads = youtube.recent_uploads(channel)
    for q in jobs(reqs, uploads):
        who, old, sec = q["streamer"], q["replace"], int(q.get("sec") or 0)
        if any(f"redo:{old}" in u["description"] for u in uploads):
            log(f"{old}: zaten yeniden yapılmış, atlandı.")
            continue
        log(f"{old}: yeniden yapılıyor | {q.get('title', '')}")
        try:
            if q.get("clip"):  # a viewer clip: its own video
                r = requests.get(f"https://kick.com/api/v2/clips/{q['clip']}", headers=H, timeout=30)
                k = r.json().get("clip") if r.ok else None
                if not k or not k.get("clip_url"):
                    log(f"{old}: izleyici kesiti {q['clip']} bulunamadı, atlandı.")
                    continue
                src, a, b, stitle = k["clip_url"], 0, int(k.get("duration") or 60), k.get("title", "")
            else:
                v = recording(who, q["stream"])
                src, stitle = v["source"], v.get("session_title", "")
                a, b = max(sec - q.get("before_s", 38), 0), sec + q.get("after_s", 15)
        except Exception as e:
            log(f"{old}: kaynak bulunamadı: {type(e).__name__}: {e}")
            continue
        stamp = f"{who}_redo_{old}"
        raw = out / f"{stamp}_raw.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(a), "-i", src, "-t", str(b - a),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", str(raw)], check=True)
        try:
            box = cam.find(llm, cam.frames([raw], per_video=3))
            log(f"kamera: {cam.where(box)} {box or ''}")
        except Exception as e:
            log(f"kamera kontrolü yapılamadı: {type(e).__name__}: {e}")
            box = cfg.get("cams", {}).get(who)  # only when unsure; "no webcam" (full-screen camera) stays None
        words = render.transcribe(raw)
        seen = render.look(raw)
        try:
            meta = describe(llm, words, who, stitle, q.get("category", ""), seen["frames"])
        except RuntimeError as e:
            log(f"kanca yazılamadı: {e}")
            meta = {}
        final = render.render(raw, out / f"{stamp}.mp4", words, meta.get("hook", ""), who, box)
        raw.unlink(missing_ok=True)
        title = q.get("title") or meta.get("title") or f"{who} yayınından #shorts"
        desc = (f"{who} canlı yayınından bir an.\nYayıncı: https://kick.com/{who}\n\n#{who} #kick #shorts\n\n"
                + (f"clip:{q['clip']}\n" if q.get("clip") else "") + f"moment:{q['stream']}:{sec}\nredo:{old}")
        if args.dry_run:
            log(f"[deneme] hazır: {final.name} | {title} | kanca: {meta.get('hook', '')}")
            continue
        vid = youtube.upload(channel, final, title, desc, meta.get("tags", []), 5)
        log(f"yeni hali yüklendi: https://youtu.be/{vid} | {title}")
        youtube.delete(channel, old)
        log(f"eski hali silindi: {old}")


if __name__ == "__main__":
    main()
