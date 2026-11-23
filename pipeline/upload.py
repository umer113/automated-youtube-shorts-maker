"""YouTube Data API upload with US prime-time scheduling (resume-safe)."""
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import CLIENT_SECRET, SECRETS, TOKEN, die, load_job, load_json, save_json
from .progress import report
from .render import write_meta

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]  # full scope: needed to delete old uploads


def delete_videos(video_ids):
    """Delete videos from the channel (e.g. before re-posting). Returns {id: 'deleted'|'not found'|error}."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    yt = build("youtube", "v3", credentials=_creds(), cache_discovery=False)
    out = {}
    for vid in video_ids:
        try:
            yt.videos().delete(id=vid).execute()
            out[vid] = "deleted"
        except HttpError as e:
            out[vid] = "not found" if e.resp.status == 404 else f"error {e.resp.status}: {str(e)[:150]}"
        print(f"[delete] {vid}: {out[vid]}", flush=True)
    return out


def _register_chrome(profile: str):
    """Register a webbrowser entry that opens Chrome in a specific profile folder."""
    import os
    import webbrowser
    candidates = [Path(os.environ.get(v, "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
                  for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    chrome = next((c for c in candidates if c.exists()), None)
    if not chrome:
        die("Chrome not found")

    class _Chrome(webbrowser.BaseBrowser):
        def open(self, url, new=0, autoraise=True):
            import subprocess
            subprocess.Popen([str(chrome), f"--profile-directory={profile}", url])
            return True

    webbrowser.register("chrome-profile", None, _Chrome("chrome-profile"))
    return "chrome-profile"


def auth(chrome_profile: str = None):
    if not CLIENT_SECRET.exists():
        die(f"{CLIENT_SECRET} not found. Download the OAuth 'Desktop app' client JSON from Google Cloud "
            "and save it there.")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    browser = _register_chrome(chrome_profile) if chrome_profile else None
    print("[auth] browser opening... log in with the channel's Google account and click Allow.", flush=True)
    creds = flow.run_local_server(port=0, prompt="consent", browser=browser)
    SECRETS.mkdir(exist_ok=True)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    print(f"[auth] saved {TOKEN}")


def _creds():
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not TOKEN.exists():
        die("Not logged in. Run: shorts.py auth")
    import json
    granted = set(json.loads(TOKEN.read_text(encoding="utf-8")).get("scopes") or [])
    if granted and not set(SCOPES) <= granted:
        die("Google login needs new permissions (delete videos). Run: shorts.py auth")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        if creds.refresh_token:
            try:
                creds.refresh(Request())
                TOKEN.write_text(creds.to_json(), encoding="utf-8")
            except RefreshError:
                die("Google login expired (happens every ~7 days in Testing mode). Run: shorts.py auth")
        else:
            die("Google login invalid. Run: shorts.py auth")
    return creds


def _slots(job, uploads, n):
    cfg = job.get("upload", {})
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    times = sorted(cfg.get("times", ["17:00", "19:00", "21:00"]))
    floor = datetime.now(timezone.utc) + timedelta(minutes=20)
    for u in uploads.values():
        if u.get("publishAt"):
            floor = max(floor, datetime.fromisoformat(u["publishAt"].replace("Z", "+00:00")))
    day = floor.astimezone(tz).date()
    if cfg.get("start_date"):
        day = max(day, datetime.strptime(cfg["start_date"], "%Y-%m-%d").date())
    out = []
    while len(out) < n:
        for t in times:
            hh, mm = map(int, t.split(":"))
            local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            if local.astimezone(timezone.utc) > floor and len(out) < n:
                out.append(local)
        day += timedelta(days=1)
    return out


def upload(job_dir: Path, dry_run: bool = False, limit: int = None, private_only: bool = False,
           public_now: bool = False):
    job = load_job(job_dir)
    clips = load_json(job_dir / "clips.json")["clips"]
    ufile = job_dir / "uploads.json"
    uploads = load_json(ufile) if ufile.exists() else {}
    out_dir = job_dir / "out"

    pending = [c for c in sorted(clips, key=lambda c: c["id"])
               if str(c["id"]) not in uploads and (out_dir / f"clip_{c['id']:02d}.mp4").exists()]
    if not pending:
        print("[upload] nothing to upload (all rendered clips already uploaded)")
        return {"uploaded": 0, "pending": 0, "quota_hit": False}
    batch = pending[:limit] if limit else pending  # no cap: stops only if YouTube refuses
    slots = _slots(job, uploads, len(batch))

    if dry_run:
        for c, slot in zip(batch, slots):
            desc, tags = write_meta(job, c, out_dir / f"clip_{c['id']:02d}.txt")
            print(f"[dry-run] clip_{c['id']:02d} -> {slot:%a %d %b %H:%M %Z} | {c['title']}")
            print(f"          desc: {desc.splitlines()[0][:90]} ... | tags: {', '.join(tags)[:80]}")
        print(f"[dry-run] {len(batch)} of {len(pending)} pending would be uploaded")
        return

    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    yt = build("youtube", "v3", credentials=_creds(), cache_discovery=False)
    count, quota_hit, t_start = 0, False, time.time()
    for k, (c, slot) in enumerate(zip(batch, slots), 1):
        name = f"clip_{c['id']:02d}"
        avg = (time.time() - t_start) / count if count else 25.0
        report(f"Uploading {k}/{len(batch)} to YouTube: \"{c['title']}\"",
               pct=count / len(batch), eta=avg * (len(batch) - count))
        desc, tags = write_meta(job, c, out_dir / f"{name}.txt")
        publish_at = slot.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {
            "snippet": {"title": c["title"][:100], "description": desc[:4900], "tags": tags,
                        "categoryId": "20", "defaultLanguage": "en", "defaultAudioLanguage": "en"},
            "status": {"privacyStatus": "private", "publishAt": publish_at,
                       "selfDeclaredMadeForKids": False},
        }
        if private_only:  # stays private, never auto-publishes
            del body["status"]["publishAt"]
            publish_at = None
        elif public_now:  # live immediately
            del body["status"]["publishAt"]
            body["status"]["privacyStatus"] = "public"
            publish_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        media = MediaFileUpload(str(out_dir / f"{name}.mp4"), mimetype="video/mp4",
                                chunksize=4 * 1024 * 1024, resumable=True)
        try:
            req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
            resp = None
            while resp is None:
                status, resp = req.next_chunk()
                if status:
                    report(None, pct=(count + status.progress()) / len(batch))
        except HttpError as e:
            msg = str(e)
            if "quotaExceeded" in msg:
                quota_hit = "quota"
                report("[upload] YouTube API daily quota used up. It resets at midnight Pacific time "
                       "(1:00 PM Pakistan time). Press Resume after that.")
                break
            if "uploadLimitExceeded" in msg:
                quota_hit = "channel"
                report("[upload] Your channel hit YouTube's daily upload limit (new channels get a lower limit). "
                       "It frees up about 24 h after today's first uploads. Press Resume tomorrow.")
                break
            die(f"upload of {name} failed: {msg[:400]}")
        vid = resp["id"]
        uploads[str(c["id"])] = {"video_id": vid, "publishAt": publish_at,
                                 "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        save_json(ufile, uploads)
        with open(job_dir / "links.txt", "a", encoding="utf-8") as f:
            f.write(f"https://youtube.com/shorts/{vid}\n")
        try:
            media.stream().close()  # release the file handle (Windows locks open files)
        except Exception:
            pass
        if job.get("cleanup", job.get("lean", True)):  # free disk: the clip is on YouTube now
            try:
                (out_dir / f"{name}.mp4").unlink(missing_ok=True)
            except OSError:
                print(f"[upload] note: couldn't delete {name}.mp4 (in use); run `shorts.py clean` later")
        count += 1
        when = ("private, not scheduled" if private_only else "public now" if public_now
                else f"scheduled {slot:%a %d %b %H:%M %Z}")
        report(f"[upload] {name} -> https://youtube.com/shorts/{vid}  ({when})", pct=count / len(batch))

    left = len(pending) - count
    print(f"[upload] uploaded {count}. {left} still pending.")
    print("[upload] NOTE: until your API project passes Google's audit, videos may stay PRIVATE. "
          "Check YouTube Studio and set them Public/Scheduled if needed.")
    return {"uploaded": count, "pending": left, "quota_hit": quota_hit}
