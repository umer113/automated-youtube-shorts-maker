"""Downloads via yt-dlp.

Full mode : download the eligible section(s) as video, then extract audio.
Lean mode : download AUDIO ONLY for the transcript; later fetch only the chosen
            clip ranges as video (fetch_clip). Much less bandwidth and disk.
"""
import subprocess
import sys
import wave
from pathlib import Path

from .config import FFMPEG, die, load_job, parse_time, require_ffmpeg, run, source_video
from .progress import report


def _friendly_error(stderr: str) -> str:
    s = stderr.lower()
    if "subscriber" in s or "sub-only" in s:
        return "This VOD is subscriber-only, so it can't be downloaded."
    if "does not exist" in s or "404" in s or "not found" in s or "unavailable" in s:
        return "The VOD looks deleted/expired/unavailable (Twitch removes old VODs)."
    if "age" in s and "restrict" in s:
        return "The video is age-restricted."
    if "geo" in s or "country" in s:
        return "The video is region-blocked."
    return "yt-dlp failed."


def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}"


def ytdlp(url: str, out_tmpl: Path, fmt: str, section: str = None, extra=(), label: str = None):
    """Run yt-dlp; stream its progress to the progress bus when `label` is given."""
    cmd = [sys.executable, "-m", "yt_dlp", url, "-f", fmt,
           "--ffmpeg-location", str(Path(FFMPEG).parent),
           "-o", str(out_tmpl), "--no-playlist", "--quiet", "--no-warnings", "--force-overwrites",
           "--newline", "--progress",
           "--progress-template", "download:PROG|%(progress._percent_str)s|%(progress.eta)s", *extra]
    if section:
        cmd += ["--download-sections", section]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    tail = []
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("PROG|"):
            if label:
                _, pct_s, eta_s = (line.split("|") + ["", ""])[:3]
                try:
                    pct = float(pct_s.strip().rstrip("%")) / 100
                except ValueError:
                    pct = None
                try:
                    eta = float(eta_s)
                except ValueError:
                    eta = None
                pct_txt = f" {pct * 100:.0f}%" if pct is not None else ""
                report(f"{label}{pct_txt}", pct=pct, eta=eta, quiet=True)
        elif line:
            tail = (tail + [line])[-8:]
    proc.wait()
    if proc.returncode != 0:
        text = "\n".join(tail)
        die(f"{_friendly_error(text)}\n" + "\n".join(tail[-4:]))


def _section(s: dict):
    start, end = s.get("start"), s.get("end")
    if start or end:
        return f"*{start or '0'}-{end or 'inf'}"
    print("[download] WARNING: no start/end given -> using the WHOLE VOD")
    return None


def vod_duration(url: str) -> float:
    """Total VOD length in seconds (metadata only, no download)."""
    proc = subprocess.run([sys.executable, "-m", "yt_dlp", url, "--skip-download", "--no-warnings",
                           "--print", "duration"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return float(proc.stdout.strip().splitlines()[-1])
    except Exception:
        die(f"{_friendly_error(proc.stderr)}\n{proc.stderr.strip()[-300:]}")


def plan_sources(job_dir: Path, budget_secs: float) -> list:
    """Limit how much stream we process: enough material for the clip count, not whole VODs.

    Fills sources in order (start -> end, or VOD end if no end given) until the budget is used.
    Rewrites job.json with explicit end times and drops sources that aren't needed.
    Returns human-readable plan lines.
    """
    from .config import fmt_time, save_json
    job = load_job(job_dir)
    job.setdefault("all_sources", job["sources"])  # the campaign's full eligible footage
    used = job.get("used", [])                       # windows already clipped by earlier batches

    def base(url):
        return url.split("?")[0].rstrip("/")

    remaining, kept, lines = budget_secs, [], []
    for s in job["all_sources"]:
        name = s.get("label") or "VOD"
        if remaining < 300 and kept:
            lines.append(f"skipping {name} (enough material already)")
            continue
        start = parse_time(s.get("start")) or 0.0
        end = parse_time(s.get("end"))
        if end is None:
            report(f"Checking length of {name} ...")
            end = vod_duration(s["url"])
        segs = [(start, end)]
        for u in used:  # cut out footage that earlier batches already used
            if base(u["url"]) != base(s["url"]):
                continue
            us, ue = parse_time(u.get("start")) or 0.0, parse_time(u.get("end")) or 1e12
            nxt = []
            for a, b in segs:
                if ue <= a or us >= b:
                    nxt.append((a, b))
                else:
                    if us > a:
                        nxt.append((a, us))
                    if ue < b:
                        nxt.append((ue, b))
            segs = nxt
        for a, b in segs:
            if b - a < 120 or (remaining < 300 and kept):
                continue
            take = min(b - a, remaining)
            kept.append(dict(s, start=fmt_time(a), end=fmt_time(a + take)))
            remaining -= take
            lines.append(f"{name}: {fmt_time(a)} -> {fmt_time(a + take)} ({take / 60:.0f} min)"
                         + (" (new footage)" if used else ""))
    if not kept:
        die("All eligible footage from this campaign has already been used, so there's nothing new to clip."
            if used else "None of the VOD links had any usable footage.")
    job["sources"] = kept
    job["planned"] = True
    save_json(job_dir / "job.json", job)
    return lines


def wav_duration(wav: Path) -> float:
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / w.getframerate()


def download(job_dir: Path, force: bool = False):
    require_ffmpeg()
    job = load_job(job_dir)
    lean = job.get("lean", True)
    src_dir = job_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    for i, s in enumerate(job["sources"]):
        wav = src_dir / f"source_{i}.wav"
        name = s.get("label") or f"VOD {i + 1}"
        if wav.exists() and not force:
            want = (parse_time(s.get("end")) or 0) - (parse_time(s.get("start")) or 0)
            if want > 0 and wav_duration(wav) > want + 5:  # older/longer download: trim to the plan
                tmp = wav.with_suffix(".trim.wav")
                run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", wav, "-t", f"{want:.2f}",
                     "-c", "copy", tmp], what="audio trim")
                tmp.replace(wav)
            report(f"[download] {name}: audio already downloaded")
            continue
        if lean:
            report(f"[download] {name}: downloading audio only (for the transcript) ...", pct=0)
            # exact cut matters: transcript time 0 must equal the section start
            ytdlp(s["url"], src_dir / f"source_{i}_audio.%(ext)s", "ba/b", _section(s),
                  ["--force-keyframes-at-cuts"], label=f"Downloading audio of {name}")
            audio = next((p for p in src_dir.glob(f"source_{i}_audio.*") if p.suffix != ".part"), None)
            if not audio:
                die(f"source {i}: audio download produced no file")
            run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", audio,
                 "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], what="audio extract")
            audio.unlink(missing_ok=True)
        else:
            video = source_video(job_dir, i)
            if not video or force:
                print(f"[download] source {i}: video {s['url']} ...", flush=True)
                ytdlp(s["url"], src_dir / f"source_{i}.%(ext)s", "bv*[height<=1080]+ba/b[height<=1080]/b",
                      _section(s), ["--merge-output-format", "mp4"])
                video = source_video(job_dir, i)
                if not video:
                    die(f"source {i}: download finished but no video file found")
            run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", video,
                 "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], what="audio extract")
        report(f"[download] {name}: audio ready ({wav_duration(wav) / 60:.1f} min of stream)", pct=1)


_url_cache: dict = {}


def stream_inputs(job: dict, src_index: int, start: float, end: float) -> tuple:
    """ffmpeg input args that read [start, end] straight from the stream (no separate download/encode).

    Returns (args, audio_input_index). Direct URLs expire after a few hours, so they're cached briefly.
    """
    import time as _t
    s = job["sources"][src_index]
    base = parse_time(s.get("start")) or 0.0
    hit = _url_cache.get(s["url"])
    if not hit or _t.time() - hit[0] > 3600:
        proc = subprocess.run([sys.executable, "-m", "yt_dlp", s["url"], "-g", "--no-warnings",
                               "-f", "bv*[height<=1080]+ba/b[height<=1080]/b"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        urls = [u for u in proc.stdout.strip().splitlines() if u.startswith("http")]
        if proc.returncode != 0 or not urls:
            die(f"{_friendly_error(proc.stderr)}\n{proc.stderr.strip()[-300:]}")
        hit = (_t.time(), urls)
        _url_cache[s["url"]] = hit
    args = []
    for u in hit[1]:
        args += ["-ss", f"{base + start:.3f}", "-t", f"{end - start:.3f}", "-i", u]
    return args, (1 if len(hit[1]) > 1 else 0)


def fetch_range(job: dict, src_index: int, start: float, end: float, out_base: Path, label: str = None) -> Path:
    """Download only [start, end] (relative to the eligible section) as video."""
    s = job["sources"][src_index]
    base = parse_time(s.get("start")) or 0.0
    ytdlp(s["url"], out_base.with_suffix(".%(ext)s"), "bv*[height<=1080]+ba/b[height<=1080]/b",
          f"*{_ts(base + start)}-{_ts(base + end)}",
          ["--merge-output-format", "mp4", "--force-keyframes-at-cuts"], label=label)
    hit = next((p for p in out_base.parent.glob(out_base.name + ".*")
                if p.suffix.lower() in (".mp4", ".mkv", ".webm")), None)
    if not hit:
        die(f"range download produced no file for {out_base.name}")
    return hit
