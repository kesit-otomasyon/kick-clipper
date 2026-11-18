"""Turn the clips viewers make on Kick into Shorts, while the stream is on (and for a few hours after).
Viewers press "clip" seconds after something funny or wild happens, so their clips are the moments
people actually care about. Every run (each 15 minutes) takes the best new clips of each streamer's
current or latest stream, makes vertical Shorts of them and uploads them, within the daily limits.

python -m clipper.viewer_clips [--streamer X] [--dry-run]"""
import argparse
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from . import cam, kick, render, youtube
from .llm import LLM
from .watch import describe, log, picture_ok, visual_ok

ROOT = Path(__file__).resolve().parent.parent


def stream_clips(slug: str, stream_id: str) -> list[dict]:
    r = requests.get(f"https://kick.com/api/v2/channels/{slug}/clips", params={"sort": "date", "time": "day"},
                     headers=kick.H, timeout=30)
    r.raise_for_status()
    return [c for c in r.json().get("clips", []) if str(c.get("livestream_id")) == str(stream_id)]


def clip_seconds(k: dict, started: str) -> tuple[int, int]:
    """(start, end) of a viewer clip in seconds from the stream start; a clip ends when it is made."""
    t0 = datetime.fromisoformat(started.replace(" ", "T").replace("Z", "+00:00"))
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    made = datetime.fromisoformat(k["created_at"].replace("Z", "+00:00"))
    end = int((made - t0).total_seconds())
    return end - int(k.get("duration") or 0), end


def latest_stream(slug: str, within_h: float):
    """The live stream, or the last one if it ended less than `within_h` hours ago."""
    live = kick.live_info(slug)
    if live:
        return live
    r = requests.get(f"https://kick.com/api/v2/channels/{slug}/videos", headers=kick.H, timeout=30)
    r.raise_for_status()
    vids = r.json()
    if not vids:
        return None
    v = vids[0]
    start = datetime.fromisoformat(v["start_time"].replace(" ", "T")).replace(tzinfo=timezone.utc)
    end = start + timedelta(milliseconds=v.get("duration") or 0)
    if datetime.now(timezone.utc) - end > timedelta(hours=within_h):
        return None
    cats = v.get("categories") or [{}]
    return {"id": str(v["id"]), "title": v.get("session_title", ""), "category": cats[0].get("name", ""),
            "started": v["start_time"]}


def score(c: dict) -> float:
    """Views per minute since the clip was made, with a floor so a brand-new clip is not over-rated."""
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))).total_seconds()
    return c.get("views", 0) / max(age / 60, 10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--streamer", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    vc = cfg["viewer_clips"]
    c = cfg["clips"]
    channel = cfg["youtube_channel"]
    uploads = [] if args.dry_run else youtube.recent_uploads(channel)
    done = {line.split(":", 1)[1].strip() for u in uploads for line in u["description"].splitlines()
            if line.startswith("clip:")}
    moments = youtube.moments_done(uploads)  # moments the live watcher (or an earlier clip) already uploaded
    today = uploads[:youtube.uploads_today(uploads)]
    total_left = c["per_day_total"] - len(today)
    llm = LLM(cfg)
    cams = {}
    out = ROOT / "out"
    out.mkdir(exist_ok=True)

    for who in [args.streamer] if args.streamer else cfg["streamers"]:
        if total_left <= 0:
            log("günlük yükleme sınırı doldu.")
            break
        mine = sum(f"kick.com/{who}" in u["description"] for u in today)
        left = min(c["per_streamer_per_day"] - mine, total_left)
        if left <= 0:
            log(f"{who}: bugünkü kesit sınırı doldu.")
            continue
        info = latest_stream(who, vc["after_stream_hours"])
        if not info:
            log(f"{who}: yayında değil (yakın zamanda da yayın yok).")
            continue
        if info["category"] in cfg.get("blocked_categories", []):
            log(f"{who}: kategori engelli ({info['category']}).")
            continue
        clips = [k for k in stream_clips(who, info["id"])
                 if k["id"] not in done and vc["min_seconds"] <= k.get("duration", 0) <= vc["max_seconds"]
                 and k.get("views", 0) >= vc["min_views"]]
        clips.sort(key=score, reverse=True)
        fresh = []
        for k in clips:
            a, b = clip_seconds(k, info["started"])
            if any(sid == info["id"] and a - 30 <= sec <= b + 15 for sid, sec in moments):
                log(f"{k['id']}: bu an zaten yüklendi (canlı izleme), atlandı.")
                done.add(k["id"])
            else:
                fresh.append(k)
        clips = fresh
        log(f"{who}: {info['title'][:50]} | {info['category']} | uygun yeni kesit: {len(clips)}")
        for k in clips[:min(left, vc["per_run"])]:
            stamp = f"{who}_{k['id']}"
            raw = out / f"{stamp}_raw.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", k["clip_url"], "-t", str(vc["max_seconds"]),
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", str(raw)],
                           check=True)
            c = cfg["clips"]
            seen, why_not = picture_ok(raw, c)
            if not seen:
                log(f"görüntü elendi ({k['id']}): {why_not}")
                raw.unlink(missing_ok=True)
                done.add(k["id"])
                continue
            if who not in cams:  # where is the webcam today? (once per streamer per run)
                try:
                    cams[who] = cam.find(llm, cam.frames([raw], per_video=3), cfg.get("cams", {}).get(who))
                    log(f"kamera: {cam.where(cams[who])} {cams[who] or ''}")
                except Exception as e:
                    log(f"kamera kontrolü yapılamadı: {type(e).__name__}: {e}")
                    cams[who] = cfg.get("cams", {}).get(who)
            words = render.transcribe(raw)
            try:
                meta = describe(llm, words, who, f"{info['title']} | kesit: {k.get('title', '')}", info["category"],
                                seen["frames"])
            except RuntimeError as e:
                log(f"başlık yazılamadı: {e}")
                continue
            if not meta.get("ok", False):
                log(f"güvenlik kontrolü reddetti ({k['id']}): {meta.get('reason', '')}")
                raw.unlink(missing_ok=True)
                done.add(k["id"])
                continue
            weak = visual_ok(meta, c)
            if weak:
                log(f"görüntü elendi ({k['id']}): {weak}")
                raw.unlink(missing_ok=True)
                done.add(k["id"])
                continue
            log(f"görüntü {meta.get('visual')}/10: {meta.get('scene', '')}")
            final = render.render(raw, out / f"{stamp}.mp4", words, meta.get("hook", ""), who, cams[who])
            raw.unlink(missing_ok=True)
            title = meta.get("title") or f"{who} yayınından #shorts"
            desc = (f"{who} canlı yayınından bir an ({info['category']}).\n"
                    f"Yayıncı: https://kick.com/{who}\n\n#{who} #kick #shorts\n\nclip:{k['id']}\n"
                    f"moment:{info['id']}:{sum(clip_seconds(k, info['started'])) // 2}")
            (out / f"{stamp}.json").write_text(json.dumps({"title": title, "desc": desc, "kick_views": k.get("views"),
                                                           "kick_title": k.get("title"), **meta},
                                                          ensure_ascii=False, indent=1), encoding="utf-8")
            if args.dry_run:
                log(f"[deneme] hazır: {final.name} | {title} | Kick'te {k.get('views')} izlenme")
            else:
                try:
                    vid = youtube.upload(channel, final, title, desc, meta.get("tags", []), c["publish_delay_min"])
                except Exception as e:
                    if not youtube.limit_hit(e):
                        raise
                    log(f"YouTube bugünkü yükleme hakkı doldu: {e}")
                    return
                log(f"yüklendi: https://youtu.be/{vid} | {title}")
                final.unlink(missing_ok=True)
                total_left -= 1
            done.add(k["id"])


if __name__ == "__main__":
    main()
