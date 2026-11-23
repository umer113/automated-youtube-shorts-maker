"""Shared paths, tool discovery and small helpers."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
CLIENT_SECRET = SECRETS / "client_secret.json"
TOKEN = SECRETS / "token.json"


def _find_tool(name: str):
    found = shutil.which(name)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        link = Path(local) / "Microsoft" / "WinGet" / "Links" / f"{name}.exe"
        if link.exists():
            return str(link)
        pattern = str(Path(local) / "Microsoft" / "WinGet" / "Packages" / "Gyan.FFmpeg*" / "*" / "bin" / f"{name}.exe")
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


def die(msg: str):
    """Abort the current step. CLI: prints + exit 1. Web runner: caught and shown in the UI."""
    raise SystemExit(f"ERROR: {msg}")


def require_ffmpeg():
    if not FFMPEG or not FFPROBE:
        die("ffmpeg/ffprobe not found. Install with: winget install --id Gyan.FFmpeg -e  (then reopen the terminal)")


def cuda_count() -> int:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count()
    except Exception:
        return 0


def run(cmd, cwd=None, what="command"):
    """Run a subprocess quietly; on failure print a short stderr tail and exit."""
    proc = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-12:])
        die(f"{what} failed (exit {proc.returncode}):\n{tail}")
    return proc


def parse_time(value) -> float:
    """Accept seconds (int/float/str) or [HH:]MM:SS[.ms]."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    parts = s.split(":")
    if not 2 <= len(parts) <= 3:
        raise ValueError(f"Bad time: {value!r}")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def fmt_time(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def load_json(path):
    with open(path, encoding="utf-8-sig") as f:  # tolerate BOM (Notepad / PowerShell 5.1)
        return json.load(f)


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def probe_duration(path) -> float:
    require_ffmpeg()
    proc = run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of",
                "default=nw=1:nk=1", path], what="ffprobe")
    return float(proc.stdout.strip())


def source_video(job_dir: Path, i: int):
    """Downloaded video for source i (extension may vary), or None."""
    for p in sorted((job_dir / "src").glob(f"source_{i}.*")):
        if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov"):
            return p
    return None


def load_job(job_dir: Path) -> dict:
    jf = job_dir / "job.json"
    if not jf.exists():
        die(f"{jf} not found")
    job = load_json(jf)
    if not job.get("sources"):
        die("job.json has no sources")
    return job
