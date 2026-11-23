"""End-to-end job runner used by the web UI: campaign text -> clips on YouTube -> links.

State lives in jobs/<id>/state.json so a page refresh / server restart keeps history.
Every step is idempotent, so "Resume" just runs the job again and skips finished work.
"""
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import progress
from .config import ROOT, cuda_count, load_json, save_json

JOBS = ROOT / "jobs"
STAGES = [
    ("parse", "Reading the campaign"),
    ("audio", "Downloading stream audio"),
    ("transcribe", "Transcribing what they say"),
    ("frames", "Checking the stream layout"),
    ("pick", "Claude picks the best moments"),
    ("render", "Making the Shorts"),
    ("upload", "Uploading to YouTube"),
]

_lock = threading.Lock()          # one job at a time (CPU/GPU heavy)
_states: dict = {}                # id -> state (in-memory mirror of state.json)


# ------------------------------------------------------------------ state helpers

def _new_state(job_id, description, n_clips, mode):
    return {
        "id": job_id, "title": "New campaign", "created": datetime.now().isoformat(timespec="seconds"),
        "status": "queued", "n_clips": n_clips, "mode": mode, "description": description,
        "stages": [{"key": k, "label": lbl, "status": "pending", "pct": 0, "msg": "", "eta": None}
                   for k, lbl in STAGES],
        "message": "Waiting to start...", "eta_total": None, "started": None, "finished": None,
        "log": [], "links": [], "error": None, "summary": None, "pending_uploads": 0,
    }


def get_state(job_id):
    if job_id in _states:
        return _states[job_id]
    f = JOBS / job_id / "state.json"
    return load_json(f) if f.exists() else None


def mark_interrupted():
    """On server start: jobs left 'running' by a previous server process can't be running now."""
    for f in JOBS.glob("*/state.json"):
        s = load_json(f)
        if s.get("status") in ("running", "queued"):
            for st in s["stages"]:
                if st["status"] == "running":
                    st["status"] = "error"
            s.update(status="error", error="Interrupted (the app was closed or restarted). Press Resume to continue.",
                     message="Interrupted - press Resume to continue")
            save_json(f, s)


def list_states():
    out = []
    for f in JOBS.glob("*/state.json"):
        jid = f.parent.name
        out.append(_states.get(jid) or load_json(f))
    return sorted(out, key=lambda s: s.get("created", ""), reverse=True)


class _Tracker:
    """Receives progress events, keeps ETA estimates, writes state.json (throttled)."""

    def __init__(self, state):
        self.s = state
        self.job_dir = JOBS / state["id"]
        self.stage = None
        self.stage_t0 = None
        self.last_save = 0
        self.gpu = cuda_count() > 0
        self.audio_secs = None
        self.n = state["n_clips"]

    # rough per-stage time estimates (seconds) used before live numbers exist
    def _estimate(self, key):
        n = self.n
        to_upload = 0 if self.s["mode"] == "none" else n
        a = self.audio_secs or 3600
        return {"parse": 40, "audio": 20 + a / 20, "transcribe": a / (60 if self.gpu else 9),
                "frames": 20, "pick": 120, "render": n * (25 if self.gpu else 90),
                "upload": to_upload * 25}[key]

    def _stage(self, key):
        return next(st for st in self.s["stages"] if st["key"] == key)

    def start(self, key):
        self.stage, self.stage_t0 = key, time.time()
        st = self._stage(key)
        st.update(status="running", pct=0, eta=self._estimate(key), live=False)
        self.s["message"] = st["label"] + "..."
        self.save(force=True)

    def done(self, key, msg=""):
        st = self._stage(key)
        st.update(status="done", pct=1, eta=0, msg=msg or st["msg"])
        self.save(force=True)

    def skip(self, key, msg):
        self._stage(key).update(status="skipped", pct=1, eta=0, msg=msg)
        self.save(force=True)

    def hook(self, msg=None, pct=None, eta=None):
        if not self.stage:
            return
        st = self._stage(self.stage)
        if pct is not None:
            st["pct"] = max(0.0, min(1.0, pct))
        el = time.time() - self.stage_t0
        if pct:
            st["live"] = True  # real progress numbers are arriving for this stage
        if eta is None and pct and pct > 0.03:
            eta = el / pct * (1 - pct)
        if eta is not None:
            st["eta"] = eta
        if msg:
            clean = re.sub(r"^\[\w+(?:-\w+)?\]\s*", "", msg)
            st["msg"] = clean
            self.s["message"] = clean
            if not re.search(r"\d+%$", clean):  # don't flood the log with % ticks
                self.s["log"] = (self.s["log"] + [{"t": datetime.now().strftime("%H:%M:%S"), "msg": clean}])[-200:]
        self.save()

    def log(self, msg):
        self.hook(msg=msg)

    def tick(self):
        """Called every 2 s: if a stage gives no live progress, count its estimate down."""
        if not self.stage:
            return
        st = self._stage(self.stage)
        if st["status"] == "running" and not st.get("live"):
            el = time.time() - self.stage_t0
            est = self._estimate(self.stage)
            st["eta"] = max(est - el, 15)
            st["pct"] = min(el / est, 0.95) if est else 0
            self.save()

    def save(self, force=False):
        # overall ETA = current stage ETA + estimates for stages not started yet
        total, seen_running = 0.0, False
        for st in self.s["stages"]:
            if st["status"] == "running":
                total += st["eta"] or 0
                seen_running = True
            elif st["status"] == "pending":
                total += self._estimate(st["key"])
        self.s["eta_total"] = round(total) if seen_running or total else None
        if force or time.time() - self.last_save > 1:
            self.last_save = time.time()
            save_json(self.job_dir / "state.json", self.s)


# ------------------------------------------------------------------ the job

def _slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "campaign"


def create_job(description: str, n_clips: int, mode: str, parent: str = None) -> str:
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    (JOBS / job_id).mkdir(parents=True, exist_ok=True)
    state = _new_state(job_id, description, n_clips, mode)
    if parent:  # "make more clips": same campaign, only footage that earlier batches didn't use
        p = get_state(parent) or {}
        state["parent"] = parent
        state["batch"] = (p.get("batch") or 1) + 1
        state["title"] = f"{p.get('title', 'Campaign')} · batch {state['batch']}"
        state["summary"] = p.get("summary")
    _states[job_id] = state
    save_json(JOBS / job_id / "state.json", state)
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()
    return job_id


def resume_job(job_id: str):
    state = get_state(job_id)
    if not state:
        raise KeyError(job_id)
    if state["status"] in ("running", "queued"):
        return
    state.update(status="queued", error=None, finished=None, message="Resuming...")
    for st in state["stages"]:
        if st["status"] in ("running", "error") or st["key"] == "upload":
            st.update(status="pending", pct=0, eta=None)
    _states[job_id] = state
    save_json(JOBS / job_id / "state.json", state)
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()


def repost_job(job_id: str):
    """Re-make + re-upload every clip of a job as PUBLIC NOW (campaigns that need submitting within
    30 min of going live). Old uploads are archived, not deleted from YouTube; delete those in Studio."""
    state = get_state(job_id)
    if not state:
        raise KeyError(job_id)
    if state["status"] in ("running", "queued"):
        return
    job_dir = JOBS / job_id
    if not (job_dir / "clips.json").exists():
        raise ValueError("This job has no clips yet. Use Resume instead.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in ("uploads.json", "links.txt"):
        f = job_dir / name
        if f.exists():
            f.rename(job_dir / f"{f.stem}_old_{stamp}{f.suffix}")
    state.update(status="queued", error=None, finished=None, mode="public", links=[], pending_uploads=0,
                 message="Re-posting: re-making the clips, then uploading them public...")
    for st in state["stages"]:
        if st["key"] in ("audio", "render", "upload"):  # audio again = accurate captions
            st.update(status="pending", pct=0, eta=None, msg="")
    _states[job_id] = state
    save_json(job_dir / "state.json", state)
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()


def _links(job_dir: Path):
    ufile, cfile = job_dir / "uploads.json", job_dir / "clips.json"
    if not ufile.exists():
        return []
    ups = load_json(ufile)
    titles = {str(c["id"]): c["title"] for c in load_json(cfile)["clips"]} if cfile.exists() else {}
    return [{"id": int(k), "title": titles.get(k, f"clip {k}"), "url": f"https://youtube.com/shorts/{u['video_id']}",
             "publish_at": u.get("publishAt")} for k, u in sorted(ups.items(), key=lambda kv: int(kv[0]))]


def _run(job_id: str):
    state = _states[job_id]
    with _lock:
        tr = _Tracker(state)
        progress.set_hook(tr.hook)
        state.update(status="running", started=state.get("started") or datetime.now().isoformat(timespec="seconds"))
        job_dir = JOBS / job_id
        stop_tick = threading.Event()

        def _ticker():
            while not stop_tick.wait(2):
                tr.tick()
        threading.Thread(target=_ticker, daemon=True).start()
        try:
            _pipeline(state, tr, job_dir)
        except BaseException as e:  # SystemExit from die(), or a bug
            msg = str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
            if not isinstance(e, SystemExit):
                traceback.print_exc()
            msg = msg.replace("ERROR: ", "")
            for st in state["stages"]:
                if st["status"] == "running":
                    st["status"] = "error"
            state.update(status="error", error=msg, message="Stopped: " + msg.splitlines()[0])
            tr.log("Stopped: " + msg.splitlines()[0])
        finally:
            stop_tick.set()
            progress.set_hook(None)
            state["finished"] = datetime.now().isoformat(timespec="seconds")
            state["links"] = _links(job_dir)
            tr.save(force=True)


def _stage_done(state, key):
    return next(st for st in state["stages"] if st["key"] == key)["status"] in ("done", "skipped")


def _pipeline(state, tr, job_dir: Path):
    from .download import download
    from .frames import extract_frames
    from .llm import parse_campaign, pick_clips
    from .render import render, validate_clips
    from .transcribe import transcribe
    from .upload import upload

    n = state["n_clips"]

    # 1. campaign -> job.json
    jf = job_dir / "job.json"
    if not jf.exists() and state.get("parent"):
        tr.start("parse")
        pjob = load_json(JOBS / state["parent"] / "job.json")
        all_sources = pjob.get("all_sources")
        if not all_sources:  # older job: re-read the description to get the full VOD list
            tr.log("Re-reading the campaign to get the full VOD list...")
            all_sources = parse_campaign(state["description"]).get("sources") or pjob["sources"]
        used = pjob.get("used", []) + [{"url": s["url"], "start": s.get("start"), "end": s.get("end")}
                                        for s in pjob["sources"]]
        job = dict(pjob, sources=all_sources, all_sources=all_sources, used=used, planned=False, max_clips=n)
        save_json(jf, job)
        tr.done("parse", f"same campaign as batch {state['batch'] - 1} · skipping footage already clipped")
    if not jf.exists():
        tr.start("parse")
        tr.log("Claude is reading the campaign description and rules...")
        info = parse_campaign(state["description"])
        if not info.get("youtube_allowed", True):
            raise SystemExit("ERROR: This campaign doesn't allow YouTube Shorts. Pick another campaign.")
        if not info.get("sources"):
            raise SystemExit("ERROR: No VOD links found in the description. " + (info.get("missing") or
                             "Paste the full description including the VOD links + timestamps."))
        job = {"name": info.get("name") or _slugify(info.get("title", "")), "lean": True,
               "campaign_summary": info.get("campaign_summary", ""), "title": info.get("title", ""),
               "sources": info["sources"], "creators": info.get("creators", []),
               "hashtags": info.get("hashtags", []), "extra_tags": info.get("extra_tags", []),
               "rules": info.get("rules", []), "focus": info.get("focus", ""), "max_clips": n, "music": None,
               "caption_line": info.get("caption_line") or "",
               "upload": {"timezone": "America/New_York", "times": ["17:00", "19:00", "21:00"], "start_date": None}}
        save_json(jf, job)
        state["title"] = job["title"] or job["name"]
        state["summary"] = {"creators": job["creators"], "hashtags": job["hashtags"],
                            "vods": len(job["sources"]), "rules": job["rules"][:8],
                            "campaign": job["campaign_summary"]}
        tr.done("parse", f"{len(job['sources'])} VOD(s) · creators: {', '.join(job['creators']) or '-'} · "
                         f"hashtags: {' '.join(job['hashtags']) or '-'}")
    elif not _stage_done(state, "parse"):
        tr.done("parse", "already read")

    # 2-4. audio, transcript, frames
    budget = min(max(n * 8 * 60, 30 * 60), 4 * 3600)  # ~8 min of stream per clip, 30 min..4 h
    tr.audio_secs = budget
    if not _stage_done(state, "audio"):
        tr.start("audio")
        if not load_json(jf).get("planned"):
            from .download import plan_sources
            for line in plan_sources(job_dir, budget):
                tr.log(line)
        download(job_dir)
        from .download import wav_duration
        tr.audio_secs = sum(wav_duration(p) for p in (job_dir / "src").glob("source_*.wav"))
        tr.done("audio", f"{tr.audio_secs / 60:.0f} min of stream audio")
    if not _stage_done(state, "transcribe"):
        tr.start("transcribe")
        transcribe(job_dir)
        lines = (job_dir / "transcript.txt").read_text(encoding="utf-8").count("\n")
        tr.done("transcribe", f"{lines} lines of speech")
    if not _stage_done(state, "frames"):
        tr.start("frames")
        extract_frames(job_dir)
        tr.done("frames", "layout frames captured")

    # 5. Claude picks clips
    job = load_json(jf)
    if not (job_dir / "clips.json").exists():
        tr.start("pick")
        tr.log(f"Claude is reading the whole transcript to find the {n} best moments...")
        data = pick_clips(job_dir, n, lambda d: validate_clips(job_dir, job, d))
        tr.done("pick", f"{len(data['clips'])} moments chosen")
    elif not _stage_done(state, "pick"):
        tr.done("pick", "already chosen")
    clips = load_json(job_dir / "clips.json")["clips"]
    tr.n = len(clips)

    # 6. render
    if not _stage_done(state, "render"):
        tr.start("render")
        render(job_dir)
        tr.done("render", f"{len(clips)} Shorts ready")

    # 7. upload
    if state["mode"] == "none":
        tr.skip("upload", "upload turned off; files are in jobs/" + job_dir.name + "/out")
        state.update(status="done", message="Done. Shorts are saved on your PC (not uploaded).")
        return
    tr.start("upload")
    res = upload(job_dir, private_only=state["mode"] == "private",
                 public_now=state["mode"] == "public")
    state["links"] = _links(job_dir)
    state["pending_uploads"] = res["pending"]
    if res["pending"]:
        tr.done("upload", f"{len(state['links'])} uploaded · {res['pending']} waiting (YouTube daily limit)")
        tr._stage("upload")["status"] = "waiting"
        why = {"quota": "YouTube API daily quota is used up; it resets at 1:00 PM Pakistan time (midnight Pacific).",
               "channel": "Your channel hit YouTube's daily upload limit; it frees up ~24 h after today's first uploads."
               }.get(res.get("quota_hit"), "YouTube stopped accepting uploads for now.")
        state.update(status="waiting",
                     message=f"{len(state['links'])} uploaded, {res['pending']} waiting. {why} "
                             f"Then press Resume. The clips are saved, nothing gets re-made.")
    else:
        tr.done("upload", f"{len(state['links'])} uploaded")
        hurry = " They're live now: submit the links to the campaign within 30 minutes!" \
            if state["mode"] == "public" else ""
        state.update(status="done", message=f"All done! {len(state['links'])} Shorts uploaded.{hurry}")
        import shutil
        shutil.rmtree(job_dir / "src", ignore_errors=True)  # everything is on YouTube; free the disk
