"""faster-whisper transcription + loudness markers -> compact transcript.txt."""
import os
import time
import wave
from pathlib import Path

import numpy as np

from .progress import report
from .config import cuda_count, fmt_time, load_job, load_json, parse_time, probe_duration, save_json, source_video


def _loudness_per_second(wav_path: Path) -> np.ndarray:
    """RMS level in dB for each second of a 16-bit mono wav (read in chunks)."""
    out = []
    with wave.open(str(wav_path), "rb") as w:
        rate = w.getframerate()
        while True:
            raw = w.readframes(rate)
            if not raw:
                break
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
            out.append(20 * np.log10(rms + 1e-9))
    return np.array(out)


def _read_wav(wav_path: Path) -> np.ndarray:
    """16 kHz mono int16 wav -> float32 array (avoids PyAV decode issues)."""
    with wave.open(str(wav_path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _transcribe_source(model, wav: Path, gpu: bool):
    segments, info = model.transcribe(
        _read_wav(wav), language="en", vad_filter=True, word_timestamps=True,
        condition_on_previous_text=False, beam_size=5 if gpu else 1)
    segs, words = [], []
    t0, last_print, last_hook = time.time(), 0.0, 0.0
    total = max(info.duration, 1)
    for seg in segments:
        segs.append({"s": round(seg.start, 2), "e": round(seg.end, 2), "t": seg.text.strip()})
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"w": text, "s": round(w.start, 2), "e": round(w.end, 2)})
        pct = min(seg.end / total, 1.0)
        elapsed = time.time() - t0
        eta = elapsed / pct * (1 - pct) if pct > 0.02 else None
        if time.time() - last_hook >= 1:
            last_hook = time.time()
            report(f"Listening to the stream: {fmt_time(seg.end)} of {fmt_time(total)} transcribed",
                   pct=pct, eta=eta, quiet=True)
        if seg.end - last_print >= 600:
            last_print = seg.end
            print(f"[transcribe]   {fmt_time(seg.end)} / {fmt_time(total)} ({pct * 100:.0f}%, "
                  f"{elapsed / 60:.1f} min elapsed)", flush=True)
    return segs, words


_caption_model = None


def clip_words(job_dir: Path, src: int, start: float, end: float):
    """Accurate word timings for one clip (captions). Returns None if the audio is gone or on GPU
    (the GPU full-stream pass already uses the big model)."""
    global _caption_model
    wav = job_dir / "src" / f"source_{src}.wav"
    if not wav.exists() or cuda_count() > 0:
        return None
    pad = 0.5
    a, b = max(0.0, start - pad), end + pad
    with wave.open(str(wav), "rb") as w:
        rate = w.getframerate()
        w.setpos(min(int(a * rate), w.getnframes()))
        raw = w.readframes(int((b - a) * rate))
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if _caption_model is None:
        from faster_whisper import WhisperModel
        _caption_model = WhisperModel("small.en", device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    segments, _ = _caption_model.transcribe(audio, language="en", vad_filter=True, word_timestamps=True,
                                            condition_on_previous_text=False, beam_size=5)
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"w": text, "s": round(w.start + a, 2), "e": round(w.end + a, 2)})
    return words


def _build_lines(segs, loud):
    """Merge whisper segments into ~6-12 s lines and flag loud ones with '!'."""
    thresh = float(np.percentile(loud, 90)) if loud.size else 0.0
    lines, cur = [], None
    for seg in segs:
        if cur and (seg["e"] - cur["s"] > 12 or cur["e"] - cur["s"] >= 6 or seg["s"] - cur["e"] > 2):
            lines.append(cur)
            cur = None
        if cur is None:
            cur = dict(seg)
        else:
            cur["e"] = seg["e"]
            cur["t"] += " " + seg["t"]
    if cur:
        lines.append(cur)
    for ln in lines:
        a, b = int(ln["s"]), max(int(ln["s"]) + 1, int(np.ceil(ln["e"])))
        window = loud[a:b]
        ln["loud"] = bool(window.size and window.max() >= thresh)
    return lines


def transcribe(job_dir: Path, model_name: str = None, force: bool = False):
    job = load_job(job_dir)
    gpu = cuda_count() > 0
    # CPU: a fast model is enough to FIND moments; captions get re-transcribed per clip (clip_words)
    model_name = model_name or ("large-v3-turbo" if gpu else "base.en")
    model = None

    out_lines = []
    for i, s in enumerate(job["sources"]):
        wav = job_dir / "src" / f"source_{i}.wav"
        if not wav.exists():
            raise SystemExit(f"ERROR: {wav} missing, run download first")
        words_file = job_dir / f"words_{i}.json"
        if words_file.exists() and not force:
            data = load_json(words_file)
            print(f"[transcribe] source {i}: already transcribed")
        else:
            if model is None:
                from faster_whisper import WhisperModel
                report(f"[transcribe] loading speech model ({model_name} on {'GPU' if gpu else 'CPU'}) ...")
                model = WhisperModel(model_name, device="cuda" if gpu else "cpu",
                                     compute_type="float16" if gpu else "int8",
                                     cpu_threads=os.cpu_count() or 4)
            print(f"[transcribe] source {i}: transcribing ...", flush=True)
            t0 = time.time()
            segs, words = _transcribe_source(model, wav, gpu)
            with wave.open(str(wav), "rb") as w:
                wav_dur = w.getnframes() / w.getframerate()
            data = {"duration": round(wav_dur, 2), "segments": segs, "words": words}
            save_json(words_file, data)
            report(f"[transcribe] source {i}: done in {(time.time() - t0) / 60:.1f} min, {len(words)} words", pct=1)

        loud = _loudness_per_second(wav)
        video = source_video(job_dir, i)
        dur = probe_duration(video) if video else len(loud)
        start = parse_time(s.get("start")) or 0
        out_lines.append(f"# source {i} | {s.get('label', '')} | file offset = VOD {fmt_time(start)} | "
                         f"duration {fmt_time(dur)}")
        for ln in _build_lines(data["segments"], loud):
            mark = "! " if ln["loud"] else ""
            out_lines.append(f"[{i} {fmt_time(ln['s'])}-{fmt_time(ln['e'])}] {mark}{ln['t']}")
        out_lines.append("")

    text = "\n".join(out_lines)
    (job_dir / "transcript.txt").write_text(text, encoding="utf-8")
    print(f"[transcribe] transcript.txt written: {len(out_lines)} lines, ~{len(text) // 4 // 1000}K tokens")
