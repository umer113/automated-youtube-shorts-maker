"""Claude Code (headless `claude -p`) as the LLM. Uses the user's Claude subscription login.

Kept cheap: custom short system prompt, only the Read tool (for frame images), no MCP,
run from a neutral folder so the project's CLAUDE.md workflow isn't loaded.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import die, load_json, save_json

MODEL = os.environ.get("SHORTS_MODEL")  # optional override, e.g. "sonnet" / "opus"


def _claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude.exe"
    if local.exists():
        return str(local)
    die("Claude Code CLI not found. Install it (https://claude.com/claude-code) and log in once with `claude`.")


def _extract_json(text: str):
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(raw)
    except Exception:
        die(f"Claude did not return valid JSON. Start of reply:\n{text[:400]}")


def ask_claude(system: str, prompt: str, read_dirs=(), timeout: int = 900) -> dict:
    """Send one prompt (stdin) and return the parsed JSON object from the reply."""
    cmd = [_claude_bin(), "-p", "--output-format", "json", "--max-turns", "6",
           "--system-prompt", system, "--strict-mcp-config"]
    if read_dirs:
        cmd += ["--tools", "Read", "--allowedTools", "Read"]
        for d in read_dirs:
            cmd += ["--add-dir", str(d)]
    else:
        cmd += ["--tools", ""]
    if MODEL:
        cmd += ["--model", MODEL]
    cwd = Path(tempfile.gettempdir()) / "shorts-llm"
    cwd.mkdir(exist_ok=True)
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        die("Claude took too long to answer (timeout). Try again.")
    try:
        out = json.loads(proc.stdout)
    except Exception:
        die(f"Claude CLI failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-400:]}")
    if out.get("is_error"):
        msg = out.get("result") or out.get("subtype")
        if "limit" in str(msg).lower():
            die(f"Claude usage limit reached: {msg}. Wait for the reset, then press Resume.")
        die(f"Claude error: {msg}")
    return _extract_json(out.get("result", ""))


# ---------------------------------------------------------------- campaign parsing

PARSE_SYSTEM = """You turn a clipping-campaign description into JSON for a video pipeline.
Reply with ONE JSON object only, no prose."""

PARSE_PROMPT = """Campaign description (pasted by the user):
<<<
{desc}
>>>

Return this JSON:
{{
  "name": "short-kebab-slug, e.g. sypherpk-nov15",
  "title": "Human title, e.g. SypherPK Nov 15 stream",
  "campaign_summary": "1 line: who, what to clip, pay per 1K, min views",
  "platforms": ["youtube", "tiktok", ...],
  "youtube_allowed": true,
  "sources": [{{"url": "...", "start": "HH:MM:SS or null", "end": "HH:MM:SS or null", "label": "e.g. Sypher POV"}}],
  "creators": ["names that must be mentioned"],
  "hashtags": ["#required", "#hashtags"],
  "extra_tags": ["5-8 lowercase youtube tags"],
  "caption_line": "exact sentence the rules require in every caption, e.g. 'Watch X on YouTube and Twitch' (empty if none)",
  "rules": ["short rule", "..."],
  "focus": "who/what must be the primary focus of every clip",
  "missing": "what's missing to start (e.g. 'no VOD links'), or empty string"
}}
Notes: one source per VOD link; if a link lists several eligible ranges, make one source per range.
Copy URLs exactly. Hashtags keep the '#'. If no VOD URLs are present, sources = [] and say so in "missing"."""


def parse_campaign(description: str) -> dict:
    return ask_claude(PARSE_SYSTEM, PARSE_PROMPT.format(desc=description.strip()), timeout=300)


# ---------------------------------------------------------------- clip picking

PICK_SYSTEM = """You are an expert short-form video editor picking viral moments from stream transcripts.
Reply with ONE JSON object only, no prose. Follow every campaign rule exactly."""

PICK_PROMPT = """Pick the best {n} moments for YouTube Shorts.

CAMPAIGN (rules are mandatory):
{job}

LAYOUT: Use the Read tool to look at these frame images from each source: {frames}
- If the stream has a facecam overlay: "split" with "facecam": [x, y, w, h] as FRACTIONS (0..1) of the frame,
  tight around the WHOLE webcam box incl. the person's full head (exclude borders/logos).
  The gameplay below is a SQUARE crop of the frame's full height; per clip set "game_x" (0 = left edge,
  0.5 = middle, 1 = right edge) to aim it at where the action/menu is during that moment (usually 0.5).
  The facecam overlay itself sits inside the gameplay frame, so prefer game_x that avoids it if possible.
- Otherwise "center" (full-screen 9:16 crop); per clip set "x" 0..1 aiming the crop at the action.
- Never use "blur". Avoid crops that include logos/overlays.

CLIP RULES:
- 20-60 s each (ideal 25-45). Start 1-2 s before the setup, never mid-word. End right after the payoff
  (kill, clutch, fail, laugh, punchline). Must make sense without context.
- Prefer lines marked "!" (loud: hype, screaming, laughing). Prefer the required people together.
- Skip lobby/menu/dead air, sponsor reads, drama, anything negative or misleading.
- No overlaps, no near-duplicates. Times are relative to each source file, as in the transcript.
- title: English, <= 60 chars, curiosity, includes a creator name, honest.
- hook: <= 6 words, ALL CAPS, on-screen for the first 3 s, includes a creator name where natural. No emoji.
- caption: 1 line + ALL required hashtags + a creator name.
- If fewer than {n} genuinely good moments exist, return fewer. Quality beats quantity.

Return:
{{"layouts": {{"0": {{"mode": "split", "facecam": [0.7, 0.02, 0.28, 0.3]}}}},
 "clips": [{{"id": 1, "source": 0, "start": "HH:MM:SS.s", "end": "HH:MM:SS.s", "game_x": 0.5,
            "title": "...", "hook": "...", "caption": "..."}}]}}

TRANSCRIPT ("[source start-end] text", "!" = loud):
{transcript}"""


def pick_clips(job_dir: Path, n: int, validate) -> dict:
    """Ask Claude for clips.json; re-ask once with the validation errors if needed."""
    job = load_json(job_dir / "job.json")
    frames_dir = job_dir / "frames"
    frames = sorted(str(p) for p in frames_dir.glob("*.jpg"))[:4]
    job_view = {k: job.get(k) for k in ("title", "campaign_summary", "creators", "hashtags", "rules", "focus")}
    job_view["sources"] = [{"index": i, "label": s.get("label")} for i, s in enumerate(job["sources"])]
    prompt = PICK_PROMPT.format(n=n, job=json.dumps(job_view, ensure_ascii=False, indent=1),
                                frames=", ".join(frames) or "(none)",
                                transcript=(job_dir / "transcript.txt").read_text(encoding="utf-8"))
    data = ask_claude(PICK_SYSTEM, prompt, read_dirs=[frames_dir])
    errors = validate(data)
    if errors:
        fix = (prompt + "\n\nYOUR PREVIOUS ANSWER:\n" + json.dumps(data, ensure_ascii=False) +
               "\n\nIt failed validation. Fix ALL of these and return the full corrected JSON:\n- " +
               "\n- ".join(errors))
        data = ask_claude(PICK_SYSTEM, fix, read_dirs=[frames_dir])
        errors = validate(data)
        if errors:  # drop the clips that are still invalid rather than failing the whole job
            bad = {int(m) for e in errors for m in re.findall(r"clip (\d+)", e)}
            data["clips"] = [c for c in data.get("clips", []) if c.get("id") not in bad]
            if not data["clips"] or validate(data):
                die("Claude's clips failed validation twice:\n  " + "\n  ".join(errors))
    data["clips"] = data.get("clips", [])[:n]
    save_json(job_dir / "clips.json", data)
    return data
