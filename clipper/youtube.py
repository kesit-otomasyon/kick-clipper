"""YouTube: daily quota check, recent uploads (to skip moments already uploaded), scheduled upload."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).resolve().parent.parent
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube"]
_yt = {}


def service(channel: str):
    if channel not in _yt:
        creds = Credentials.from_authorized_user_file(str(ROOT / "credentials" / f"token_{channel}.json"), SCOPES)
        if not creds.valid:
            creds.refresh(Request())
        _yt[channel] = build("youtube", "v3", credentials=creds, cache_discovery=False)
    return _yt[channel]


def recent_uploads(channel: str, n: int = 50) -> list[dict]:
    """[{title, description, published}] of the newest uploads (1-2 quota units)."""
    yt = service(channel)
    up = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0]
    pl = up["contentDetails"]["relatedPlaylists"]["uploads"]
    items = yt.playlistItems().list(part="snippet", playlistId=pl, maxResults=n).execute().get("items", [])
    return [{"title": i["snippet"]["title"], "description": i["snippet"].get("description", ""),
             "published": i["snippet"]["publishedAt"]} for i in items]


def uploads_today(uploads: list[dict]) -> int:
    """Uploads since the YouTube quota reset (midnight Pacific, ~UTC-8)."""
    now = datetime.now(timezone.utc) - timedelta(hours=8)
    reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=8)
    return sum(datetime.fromisoformat(u["published"].replace("Z", "+00:00")) >= reset for u in uploads)


def moments_done(uploads: list[dict]) -> set[tuple[str, int]]:
    """(stream id, second) of moments already uploaded, from the 'moment:' line of the descriptions."""
    out = set()
    for u in uploads:
        for sid, sec in re.findall(r"moment:(\d+):(\d+)", u["description"]):
            out.add((sid, int(sec)))
    return out


def upload(channel: str, video: Path, title: str, description: str, tags: list[str], delay_min: int) -> str:
    publish = (datetime.now(timezone.utc) + timedelta(minutes=delay_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"snippet": {"title": title[:100], "description": description[:4900], "tags": tags[:15],
                        "categoryId": "20", "defaultLanguage": "tr", "defaultAudioLanguage": "tr"},
            "status": {"privacyStatus": "private", "publishAt": publish, "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": False}}
    req = service(channel).videos().insert(part="snippet,status", body=body,
                                           media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]
