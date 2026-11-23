"""Shorts clipping pipeline CLI. See SHORTS_PIPELINE_SPEC.md and CLAUDE.md."""
import argparse
import os
import sys
import warnings
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
warnings.filterwarnings("ignore", module="huggingface_hub")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def cmd_check(_):
    from pipeline.config import CLIENT_SECRET, FFMPEG, FFPROBE, TOKEN, cuda_count
    ok = True
    print(f"ffmpeg : {FFMPEG or 'MISSING (winget install --id Gyan.FFmpeg -e)'}")
    print(f"ffprobe: {FFPROBE or 'MISSING'}")
    ok &= bool(FFMPEG and FFPROBE)
    for mod in ("yt_dlp", "faster_whisper", "numpy", "googleapiclient", "google_auth_oauthlib", "tzdata"):
        try:
            __import__(mod)
            print(f"python : {mod} ok")
        except ImportError:
            print(f"python : {mod} MISSING (pip install -r requirements.txt)")
            ok = False
    n = cuda_count()
    print(f"GPU    : {f'{n} CUDA device(s) -> large-v3-turbo' if n else 'none -> CPU small.en (slower)'}")
    print(f"YouTube: client_secret.json {'ok' if CLIENT_SECRET.exists() else 'MISSING (see spec 4.2)'}, "
          f"token.json {'ok' if TOKEN.exists() else 'missing -> run: shorts.py auth'}")
    print("READY" if ok else "NOT READY")


def cmd_auth(a):
    from pipeline.upload import auth
    auth(a.chrome_profile)


def cmd_prepare(a):
    from pipeline.download import download
    from pipeline.frames import extract_frames
    from pipeline.transcribe import transcribe
    job = Path(a.job)
    download(job, a.force)
    transcribe(job, a.model, a.force)
    extract_frames(job, a.force)
    print(f"[prepare] done. Next: read {job / 'transcript.txt'} + 1 frame, write clips.json, then render.")


def cmd_render(a):
    from pipeline.render import render
    only = {int(x) for x in a.only.split(",")} if a.only else None
    render(Path(a.job), only, a.force)


def cmd_upload(a):
    from pipeline.upload import upload
    upload(Path(a.job), a.dry_run, a.limit, a.private, a.public)


def cmd_delete(a):
    from pipeline.upload import delete_videos
    delete_videos(a.video_ids)


def cmd_clean(a):
    """Delete heavy files (audio/video). Keeps job.json, transcript, clips.json, uploads, links."""
    import shutil
    job_dir = Path(a.job)
    freed = 0
    for p in list((job_dir / "src").glob("*")) + list((job_dir / "out").glob("*.mp4")) + \
            list((job_dir / "out").glob("*.ass")):
        if p.is_file():
            freed += p.stat().st_size
            p.unlink()
    shutil.rmtree(job_dir / "src", ignore_errors=True)
    print(f"[clean] freed {freed / 1e6:.0f} MB")


def cmd_status(a):
    from pipeline.config import load_job, load_json, source_video
    job_dir = Path(a.job)
    job = load_job(job_dir)
    for i, _ in enumerate(job["sources"]):
        v = source_video(job_dir, i)
        print(f"source {i}: video {'ok' if v else 'no'}, words {'ok' if (job_dir / f'words_{i}.json').exists() else 'no'}")
    print(f"transcript.txt: {'ok' if (job_dir / 'transcript.txt').exists() else 'no'}")
    clips = load_json(job_dir / "clips.json")["clips"] if (job_dir / "clips.json").exists() else []
    ups = load_json(job_dir / "uploads.json") if (job_dir / "uploads.json").exists() else {}
    rendered = sum((job_dir / "out" / f"clip_{c['id']:02d}.mp4").exists() or str(c["id"]) in ups for c in clips)
    print(f"clips: {len(clips)} planned, {rendered} rendered, {len(ups)} uploaded")
    for cid, u in sorted(ups.items(), key=lambda kv: int(kv[0])):
        print(f"  clip {cid}: https://youtube.com/shorts/{u['video_id']}  publishAt {u['publishAt']}")


def main():
    p = argparse.ArgumentParser(prog="shorts.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sp = sub.add_parser("auth")
    sp.add_argument("--chrome-profile", default="Profile 20",
                    help='Chrome profile folder to log in with (default "Profile 20" = shaikh); "" = system default browser')
    sp.set_defaults(fn=cmd_auth)
    sp = sub.add_parser("prepare")
    sp.add_argument("job")
    sp.add_argument("--model", default=None, help="whisper model override, e.g. base.en / small.en / medium.en")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_prepare)
    sp = sub.add_parser("render")
    sp.add_argument("job")
    sp.add_argument("--only", default=None, help="comma-separated clip ids")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_render)
    sp = sub.add_parser("upload")
    sp.add_argument("job")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--limit", type=int, default=None, help="max uploads this run (default: no cap)")
    sp.add_argument("--private", action="store_true", help="upload as private with no schedule (for tests)")
    sp.add_argument("--public", action="store_true", help="publish immediately (no schedule)")
    sp.set_defaults(fn=cmd_upload)
    sp = sub.add_parser("status")
    sp.add_argument("job")
    sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("delete", help="delete videos from the channel by id")
    sp.add_argument("video_ids", nargs="+")
    sp.set_defaults(fn=cmd_delete)
    sp = sub.add_parser("clean")
    sp.add_argument("job")
    sp.set_defaults(fn=cmd_clean)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
