"""Local web UI: paste a campaign, pick how many clips, watch progress, get links.

Run:  start.bat   (or .\\.venv\\Scripts\\python.exe web\\server.py)  ->  http://localhost:8790
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from pipeline import runner  # noqa: E402
from pipeline.config import CLIENT_SECRET, FFMPEG, TOKEN  # noqa: E402

app = FastAPI(title="ClutchClips")
runner.mark_interrupted()
INDEX = Path(__file__).with_name("index.html")


class NewJob(BaseModel):
    description: str = Field(min_length=20)
    clips: int = Field(ge=1, le=40)
    mode: str = Field(pattern="^(public|schedule|private|none)$")


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/api/health")
def health():
    return {"ffmpeg": bool(FFMPEG), "youtube_login": TOKEN.exists(), "client_secret": CLIENT_SECRET.exists()}


@app.get("/api/jobs")
def jobs():
    return [{k: s.get(k) for k in ("id", "title", "created", "status", "n_clips", "eta_total", "message")}
            | {"links": len(s.get("links") or [])} for s in runner.list_states()]


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    s = runner.get_state(job_id)
    if not s:
        raise HTTPException(404, "job not found")
    return s


@app.post("/api/jobs")
def new_job(body: NewJob):
    if body.mode != "none" and not TOKEN.exists():
        raise HTTPException(400, "YouTube login missing. Run: shorts.py auth")
    return {"id": runner.create_job(body.description, body.clips, body.mode)}


class MoreJob(BaseModel):
    clips: int = Field(ge=1, le=40)
    mode: str = Field(pattern="^(public|schedule|private|none)$")


@app.post("/api/jobs/{job_id}/more")
def more(job_id: str, body: MoreJob):
    """New batch from the same campaign, using footage earlier batches didn't use."""
    s = runner.get_state(job_id)
    if not s:
        raise HTTPException(404, "job not found")
    if not (runner.JOBS / job_id / "job.json").exists():
        raise HTTPException(400, "This campaign never got past reading the description. Start it again instead.")
    if body.mode != "none" and not TOKEN.exists():
        raise HTTPException(400, "YouTube login missing. Run: shorts.py auth")
    return {"id": runner.create_job(s["description"], body.clips, body.mode, parent=job_id)}


@app.post("/api/jobs/{job_id}/repost")
def repost(job_id: str):
    """Re-make and re-upload all clips of a job as public now (old YouTube uploads stay; delete them in Studio)."""
    if not TOKEN.exists():
        raise HTTPException(400, "YouTube login missing. Run: shorts.py auth")
    try:
        runner.repost_job(job_id)
    except KeyError:
        raise HTTPException(404, "job not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume")
def resume(job_id: str):
    try:
        runner.resume_job(job_id)
    except KeyError:
        raise HTTPException(404, "job not found")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("SHORTS_PORT", "8790"))
    print(f"ClutchClips UI -> http://localhost:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
