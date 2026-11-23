"""Cut clips, build the 9:16 layout, burn captions."""
import subprocess
import time
from pathlib import Path

from .captions import build_ass
from .download import fetch_range, stream_inputs, wav_duration
from .transcribe import clip_words
from .progress import report
from .config import (FFMPEG, ROOT, cuda_count, die, load_job, load_json, parse_time, probe_duration,
                     require_ffmpeg, run, source_video)


def _encoder_args():
    if cuda_count() > 0:
        try:
            enc = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
            if "h264_nvenc" in enc:
                return ["-c:v", "h264_nvenc", "-preset", "p7", "-cq", "18", "-b:v", "0", "-profile:v", "high"]
        except Exception:
            pass
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high"]


SPLIT_TOP, SPLIT_BOT = 840, 1080  # facecam 1080x840 on top, square gameplay 1080x1080 below


def _video_filter(layout: dict, ass_name: str, clip: dict = None) -> str:
    """Per-clip focus: clip["game_x"] (split) / clip["x"] (center), 0 = left .. 1 = right."""
    clip = clip or {}
    mode = layout.get("mode", "center")
    if mode == "split":
        fc = layout.get("facecam")
        if not fc:
            die("split layout needs a facecam box [x, y, w, h]")
        x, y, w, h = fc
        if layout.get("game"):
            gx, gy, gw, gh = layout["game"]
            game = (f"[0:v]crop=iw*{gw}:ih*{gh}:iw*{gx}:ih*{gy},"
                    f"scale=1080:{SPLIT_BOT}:force_original_aspect_ratio=increase,crop=1080:{SPLIT_BOT}[bot]")
        else:
            gx = float(clip.get("game_x", layout.get("game_x", 0.5)))
            game = f"[0:v]crop=ih:ih:(iw-ih)*{gx}:0,scale=1080:{SPLIT_BOT}:flags=lanczos[bot]"
        return (f"[0:v]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
                f"scale=1080:{SPLIT_TOP}:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:{SPLIT_TOP}[top];"
                f"{game};[top][bot]vstack=inputs=2,setsar=1,ass={ass_name}[v]")
    if mode == "blur":  # only if explicitly asked for
        return ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2[bg];"
                f"[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,ass={ass_name}[v]")
    # "center" (default): full-screen 9:16 crop like normal Shorts.
    x = float(clip.get("x", layout.get("x", 0.5)))
    return (f"[0:v]crop=ih*9/16:ih:(iw-ih*9/16)*{x}:0,scale=1080:1920:flags=lanczos,setsar=1,"
            f"ass={ass_name}[v]")


def _validate(job, clips, layouts, durations):
    errors = []
    tags = [h.lower() for h in job.get("hashtags", [])]
    creators = [c.lower() for c in job.get("creators", [])]
    by_source = {}
    for c in clips:
        cid = c.get("id")
        try:
            s, e = parse_time(c["start"]), parse_time(c["end"])
        except Exception as ex:
            errors.append(f"clip {cid}: bad start/end ({ex})")
            continue
        src = int(c.get("source", 0))
        d = e - s
        if not 15 <= d <= 60:
            errors.append(f"clip {cid}: duration {d:.1f}s (must be 15-60s)")
        if src not in durations:
            errors.append(f"clip {cid}: source {src} not downloaded")
        elif s < 0 or e > durations[src] + 0.5:
            errors.append(f"clip {cid}: outside source length {durations[src]:.0f}s")
        if str(src) not in layouts:
            errors.append(f"clip {cid}: no layout for source {src}")
        if len(c.get("title", "")) > 100 or not c.get("title"):
            errors.append(f"clip {cid}: title missing or > 100 chars")
        if len((c.get("hook") or "").split()) > 6:
            errors.append(f"clip {cid}: hook > 6 words")
        cap = (c.get("caption") or "").lower()
        missing = [t for t in tags if t not in cap]
        if missing:
            errors.append(f"clip {cid}: caption missing {missing}")
        text = cap + " " + (c.get("hook") or "").lower()
        if creators and not any(n in text for n in creators):
            errors.append(f"clip {cid}: caption/hook must mention a creator ({job['creators']})")
        by_source.setdefault(src, []).append((s, e, cid))
    for src, items in by_source.items():
        items.sort()
        for (s1, e1, a), (s2, e2, b) in zip(items, items[1:]):
            if s2 < e1:
                errors.append(f"clips {a} and {b} overlap")
    ids = [c.get("id") for c in clips]
    if len(ids) != len(set(ids)):
        errors.append("duplicate clip ids")
    return errors


def source_durations(job_dir: Path, job: dict) -> dict:
    """Length (s) of each source's eligible section, from video, wav, or the saved transcript."""
    lean = job.get("lean", True)
    durations = {}
    for i in range(len(job["sources"])):
        v = None if lean else source_video(job_dir, i)
        wav = job_dir / "src" / f"source_{i}.wav"
        words_file = job_dir / f"words_{i}.json"
        if v:
            durations[i] = probe_duration(v)
        elif wav.exists():
            durations[i] = wav_duration(wav)
        elif words_file.exists():
            wd = load_json(words_file)
            durations[i] = wd.get("duration") or (wd["words"][-1]["e"] if wd["words"] else 0)
    return durations


def validate_clips(job_dir: Path, job: dict, data: dict) -> list:
    return _validate(job, data.get("clips", []), data.get("layouts", {}), source_durations(job_dir, job))


def write_meta(job, clip, path: Path):
    hashtags = " ".join(job.get("hashtags", []))
    creators = ", ".join(job.get("creators", []))
    line = job.get("caption_line", "").strip()  # required sentence, e.g. "Watch X on YouTube and Twitch"
    caption = clip["caption"]
    if line and line.lower() not in caption.lower():
        caption = f"{caption}\n{line}"
    desc = f"{caption}\n\n{hashtags}\n\nCredit: {creators}".strip()
    tags = list(dict.fromkeys(job.get("extra_tags", []) + job.get("creators", [])))
    path.write_text(f"TITLE:\n{clip['title']}\n\nDESCRIPTION:\n{desc}\n\nTAGS:\n{', '.join(tags)}\n",
                    encoding="utf-8")
    return desc, tags


def render(job_dir: Path, only=None, force: bool = False):
    require_ffmpeg()
    job = load_job(job_dir)
    cfile = job_dir / "clips.json"
    if not cfile.exists():
        die("clips.json not found (Claude writes it after reading transcript.txt)")
    data = load_json(cfile)
    clips, layouts = data.get("clips", []), data.get("layouts", {})

    lean = job.get("lean", True)
    videos = {} if lean else {i: source_video(job_dir, i) for i in range(len(job["sources"]))}
    errors = _validate(job, clips, layouts, source_durations(job_dir, job))
    if errors:
        die("clips.json validation failed:\n  " + "\n  ".join(errors))
    ufile = job_dir / "uploads.json"
    uploaded = set(load_json(ufile)) if ufile.exists() else set()

    out_dir = job_dir / "out"
    out_dir.mkdir(exist_ok=True)
    music = job.get("music")
    music_path = (ROOT / music["file"]).resolve() if music and music.get("file") else None
    if music_path and not music_path.exists():
        die(f"music file not found: {music_path}")
    enc = _encoder_args()
    words_cache = {}
    ov = job.get("overlay")  # {"file": "assets/x.png", "width": 760, "y": 170}: logo on screen the whole clip
    ov_path = (ROOT / ov["file"]).resolve() if ov and ov.get("file") else None
    if ov_path and not ov_path.exists():
        die(f"overlay file not found: {ov_path}")

    todo = [c for c in clips if (not only or c["id"] in only)
            and (force or (str(c["id"]) not in uploaded and not (out_dir / f"clip_{c['id']:02d}.mp4").exists()))]
    for c in clips:
        write_meta(job, c, out_dir / f"clip_{c['id']:02d}.txt")
    done, t_start = 0, time.time()
    for k, c in enumerate(todo, 1):
        cid, src = c["id"], int(c.get("source", 0))
        name = f"clip_{cid:02d}"
        avg = (time.time() - t_start) / done if done else None
        eta = avg * (len(todo) - done) if avg else None
        report(f"Clip {k}/{len(todo)}: \"{c['title']}\"", pct=done / len(todo), eta=eta, quiet=True)
        start, end = parse_time(c["start"]), parse_time(c["end"])
        layout = layouts[str(src)]
        report(f"Clip {k}/{len(todo)}: writing word-by-word captions", pct=done / len(todo), eta=eta)
        words = clip_words(job_dir, src, start, end)  # accurate model on just this clip
        if words is None:  # no audio on disk any more: use the full-stream transcript
            if src not in words_cache:
                words_cache[src] = load_json(job_dir / f"words_{src}.json")["words"]
            words = words_cache[src]
        (out_dir / f"{name}.ass").write_text(
            build_ass(words, start, end, c.get("hook", ""), layout.get("mode", "center")), encoding="utf-8")

        def build_cmd(in_args, a_idx):
            cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *in_args]
            fc = _video_filter(layout, f"{name}.ass", c)
            n_in = sum(1 for x in in_args if x == "-i")
            if ov_path:  # single image; overlay repeats its last frame for the whole clip
                cmd += ["-i", ov_path]
                head, _, _ = fc.rpartition("[v]")
                fc = (f"{head}[vb];[{n_in}:v]scale={int(ov.get('width', 760))}:-1,format=rgba[ol];"
                      f"[vb][ol]overlay=(W-w)/2:{int(ov.get('y', 170))}:format=auto,format=yuv420p[v]")
                n_in += 1
            if music_path:
                cmd += ["-stream_loop", "-1", "-i", music_path]
                vol = float(music.get("volume", 0.12))
                fc += f";[{n_in}:a]volume={vol}[m];[{a_idx}:a][m]amix=inputs=2:duration=first:normalize=0[a]"
                amap = ["-map", "[a]"]
            else:
                amap = ["-map", f"{a_idx}:a:0?"]
            return cmd + ["-filter_complex", fc, "-map", "[v]", *amap, *enc, "-pix_fmt", "yuv420p", "-r", "30",
                          "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", f"{name}.mp4"]

        report(f"Clip {k}/{len(todo)}: cutting {end - start:.0f}s from the stream, 9:16 + captions",
               pct=done / len(todo), eta=eta)
        piece = None
        if lean:
            try:  # single encode: ffmpeg reads exactly this range straight from the stream
                in_args, a_idx = stream_inputs(job, src, start, end)
                run(build_cmd(in_args, a_idx), cwd=out_dir, what=f"render {name}")
            except SystemExit:  # fall back to downloading the range first
                report(f"Clip {k}/{len(todo)}: direct read failed, downloading the range instead")
                piece = fetch_range(job, src, start, end, job_dir / "src" / name)
                run(build_cmd(["-t", f"{end - start:.3f}", "-i", piece.resolve()], 0), cwd=out_dir,
                    what=f"render {name}")
        else:
            run(build_cmd(["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", videos[src].resolve()], 0),
                cwd=out_dir, what=f"render {name}")
        if piece:
            piece.unlink(missing_ok=True)
        (out_dir / f"{name}.ass").unlink(missing_ok=True)
        done += 1
        report(f"[render] {name} ready ({end - start:.0f}s): \"{c['title']}\"", pct=done / len(todo))
    report(f"[render] done: {done} rendered -> {out_dir}", pct=1)
