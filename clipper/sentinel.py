"""Starts the live watcher as soon as a streamer goes live.

GitHub runs scheduled workflows late (often 30-120 minutes), so the first part of a stream was lost.
This runs on GitHub for ~5.8 hours, asks Kick every minute who is live, and starts watch.yml for a live
streamer that has no watcher yet. Before it stops it starts its own next shift."""
import os
import time
from pathlib import Path

import requests
import yaml

from .kick import live_info

ROOT = Path(__file__).resolve().parent.parent
REPO = os.environ.get("GITHUB_REPOSITORY", "kesit-otomasyon/kick-clipper")


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def gh(method: str, path: str, **kw) -> requests.Response:
    return requests.request(method, f"https://api.github.com/repos/{REPO}{path}", timeout=30,
                            headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                                     "Accept": "application/vnd.github+json"}, **kw)


def watching(who: str) -> bool:
    """Is a watch job for this streamer running or waiting?"""
    for status in ("in_progress", "queued", "waiting", "pending"):
        runs = gh("GET", "/actions/workflows/watch.yml/runs", params={"status": status, "per_page": 20})
        for r in runs.json().get("workflow_runs", []):
            jobs = gh("GET", f"/actions/runs/{r['id']}/jobs").json().get("jobs", [])
            if not jobs:  # the list job has not made the matrix yet: it may be for this streamer
                return True
            if any(j["name"] == f"watch ({who})" and j["status"] != "completed" for j in jobs):
                return True
    return False


def dispatch(workflow: str, inputs: dict | None = None) -> bool:
    r = gh("POST", f"/actions/workflows/{workflow}/dispatches", json={"ref": "main", "inputs": inputs or {}})
    return r.status_code == 204


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    streamers = cfg["streamers"]
    end = time.time() + float(os.environ.get("SENTINEL_HOURS", "5.8")) * 3600
    started: dict[str, float] = {}  # last time a watcher was started for a streamer
    was_live: dict[str, bool] = {}
    log(f"nöbet başladı: {', '.join(streamers)}")
    while time.time() < end:
        for who in streamers:
            try:
                live = live_info(who) is not None
            except Exception:
                continue  # Kick answered badly: ask again next minute
            if live != was_live.get(who):
                log(f"{who}: {'yayında' if live else 'yayında değil'}")
                was_live[who] = live
            if not live or time.time() - started.get(who, 0) < 600:  # give a started watcher 10 min to show up
                continue
            try:
                if watching(who):
                    started[who] = time.time()  # someone is on it: check again in 10 minutes
                    continue
                if dispatch("watch.yml", {"streamer": who}):
                    log(f"{who}: izleme başlatıldı")
                else:
                    log(f"{who}: izleme başlatılamadı")
                started[who] = time.time()
            except Exception as e:
                log(f"{who}: GitHub'a sorulamadı: {type(e).__name__}: {e}")
        time.sleep(60)
    log("vardiya bitti, sonraki nöbet başlatılıyor")
    dispatch("sentinel.yml")


if __name__ == "__main__":
    main()
