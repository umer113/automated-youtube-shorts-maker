"""Sample frames so Claude can see the stream layout."""
from pathlib import Path

from .config import FFMPEG, load_job, probe_duration, require_ffmpeg, run, source_video
from .download import fetch_range, wav_duration


def extract_frames(job_dir: Path, force: bool = False):
    require_ffmpeg()
    job = load_job(job_dir)
    fdir = job_dir / "frames"
    fdir.mkdir(exist_ok=True)
    for i, _ in enumerate(job["sources"]):
        video = source_video(job_dir, i)
        if video:  # full mode: 3 frames from the downloaded video
            dur = probe_duration(video)
            for tag, pct in (("a", 0.15), ("b", 0.50), ("c", 0.85)):
                out = fdir / f"source_{i}_{tag}.jpg"
                if out.exists() and not force:
                    continue
                run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{dur * pct:.2f}", "-i", video,
                     "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "3", out], what="frame extract")
            print(f"[frames] source {i}: frames/source_{i}_a|b|c.jpg")
            continue

        # lean mode: download 2 tiny pieces of video and grab one frame from each
        dur = wav_duration(job_dir / "src" / f"source_{i}.wav")
        for tag, pct in (("a", 0.30), ("b", 0.70)):
            out = fdir / f"source_{i}_{tag}.jpg"
            if out.exists() and not force:
                continue
            t = dur * pct
            piece = fetch_range(job, i, t, t + 2, fdir / f"_sample_{i}_{tag}")
            run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", "1", "-i", piece,
                 "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "3", out], what="frame extract")
            piece.unlink(missing_ok=True)
        print(f"[frames] source {i}: frames/source_{i}_a|b.jpg (lean)")
