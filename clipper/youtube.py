"""YouTube: daily quota check, recent uploads (to skip moments already uploaded), scheduled upload."""
import http.client
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httplib2
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


NET_ERRORS = (OSError, http.client.HTTPException, httplib2.HttpLib2Error)


def limit_hit(e: Exception) -> bool:
    """YouTube refused because the channel's (or project's) daily upload allowance is used up."""
    return any(k in str(e) for k in ("uploadLimitExceeded", "quotaExceeded", "rateLimitExceeded"))


def _retry(channel: str, call, tries: int = 4):
    """Runs call(service). A connection left idle for a long time breaks (BrokenPipe): rebuild and try again."""
    for k in range(tries):
        try:
            return call(service(channel))
        except NET_ERRORS:
            _yt.pop(channel, None)
            if k == tries - 1:
                raise
            time.sleep(5 * (k + 1))


def recent_uploads(channel: str, n: int = 50) -> list[dict]:
    """[{title, description, published}] of the newest uploads (1-2 quota units)."""
    def call(yt):
        up = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0]
        pl = up["contentDetails"]["relatedPlaylists"]["uploads"]
        return yt.playlistItems().list(part="snippet", playlistId=pl, maxResults=n).execute().get("items", [])
    items = _retry(channel, call)
    return [{"id": i["snippet"]["resourceId"]["videoId"], "title": i["snippet"]["title"],
             "description": i["snippet"].get("description", ""), "published": i["snippet"]["publishedAt"]}
            for i in items]


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
    def call(yt):
        req = yt.videos().insert(part="snippet,status", body=body,
                                 media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True))
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        return resp["id"]
    return _retry(channel, call, tries=3)


def delete(channel: str, video_id: str):
    """Deletes one of our own uploads (50 quota units)."""
    _retry(channel, lambda yt: yt.videos().delete(id=video_id).execute())
