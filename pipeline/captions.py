"""ASS subtitles: word-by-word highlighted captions + hook box."""
import re

HIGHLIGHT = r"{\c&H00FFFF&}"   # yellow (ASS is &HBBGGRR)
WHITE = r"{\c&HFFFFFF&}"

POSITIONS = {
    "split": {"hook": (540, 840), "cap": (540, 1420)},
    "blur": {"hook": (540, 360), "cap": (540, 1450)},
    "center": {"hook": (540, 360), "cap": (540, 1450)},
}

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial Black,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,6,0,5,60,60,0,1
Style: Hook,Arial Black,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,3,14,0,5,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

_NON_BMP = re.compile(r"[\U00010000-\U0010FFFF☀-➿️]")


def clean(text: str) -> str:
    """Strip emoji (fonts can't render them) and ASS control chars."""
    text = _NON_BMP.sub("", text)
    return text.replace("\\", "").replace("{", "(").replace("}", ")").strip()


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _chunks(words):
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= 3 or w["s"] - cur[-1]["e"] >= 0.6 or cur[-1]["w"][-1:] in ".?!"):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(all_words, clip_start: float, clip_end: float, hook: str, mode: str) -> str:
    pos = POSITIONS.get(mode, POSITIONS["center"])
    dur = clip_end - clip_start
    words = []
    for w in all_words:
        if w["s"] >= clip_start - 0.05 and w["e"] <= clip_end + 0.05:
            text = clean(w["w"]).upper()
            if text:
                words.append({"w": text, "s": max(0.0, w["s"] - clip_start), "e": min(dur, w["e"] - clip_start)})

    events = []
    chunks = _chunks(words)
    for ci, chunk in enumerate(chunks):
        next_chunk_start = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else dur
        for j, w in enumerate(chunk):
            start = w["s"]
            if j + 1 < len(chunk):
                end = chunk[j + 1]["s"]
            else:
                end = min(w["e"] + 0.3, next_chunk_start)
            end = max(end, start + 0.05)
            parts = [(HIGHLIGHT + x["w"] + WHITE) if k == j else x["w"] for k, x in enumerate(chunk)]
            text = rf"{{\pos({pos['cap'][0]},{pos['cap'][1]})}}" + " ".join(parts)
            events.append(f"Dialogue: 1,{_ts(start)},{_ts(end)},Caption,,0,0,0,,{text}")

    hook = clean(hook or "").upper()
    if hook:
        text = rf"{{\pos({pos['hook'][0]},{pos['hook'][1]})}}" + hook
        events.append(f"Dialogue: 2,{_ts(0)},{_ts(min(3.5, dur))},Hook,,0,0,0,,{text}")

    return HEADER + "\n".join(events) + "\n"
