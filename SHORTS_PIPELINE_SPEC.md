# YouTube Shorts Clipping Pipeline: Build Spec

> **For Claude Code:** Read this whole file, then build the project it describes in the
> current folder. Do the "One-time setup" steps with the user, build the code, run the
> test in "Acceptance test", and finally create `CLAUDE.md` (content given at the end).

---

## 1. Goal

The user joins paid **clipping campaigns** (Content Rewards / Whop). A campaign gives
permission to clip a streamer's VOD and pays per 1K views. The user wants this flow:

1. User pastes the **campaign description** into Claude Code.
2. The VOD is **downloaded automatically**, but only the eligible timestamps.
3. **Shorts are created automatically**: best moments, 9:16, burned-in captions, hook text.
4. Shorts are **uploaded to YouTube automatically** and scheduled at US prime time.
5. The user only reviews the clips and submits the video links to the campaign page by hand.

**YouTube only.** No TikTok, Instagram or X.

**Claude Code is the LLM.** No Anthropic, Gemini or OpenAI API key is used. Claude Code, in
the chat session, reads a compact transcript and writes `clips.json`. Everything heavy
(download, transcription, ffmpeg, upload) runs locally in Python and costs no tokens.

Reference project for ideas only (do not install it): https://github.com/mutonby/openshorts.
It uses yt-dlp, faster-whisper, scene detection, an LLM that picks moments, an ffmpeg cut,
a vertical reframe and captions. We copy that pipeline shape, but Claude picks the moments.

---

## 2. Hard rules (campaign plus platform safety)

These rules come from a real campaign (SypherPK, Nov 15 stream). Every campaign has its
own list, so always parse the pasted description and store its rules in `job.json`.

- Use only the **VOD links and eligible timestamps** from the campaign description.
- The clip's primary focus must match the campaign. Example: "Sypher, Replays and Maddynf
  all playing together".
- The **caption must include the required hashtags**. Example: `#fortnitecreative #uefn`.
- The caption or on-screen text **must name at least one creator**.
- **English only** for captions, titles and on-screen text.
- **No logos of any kind.** Never add a watermark or channel logo. Prefer crops that
  exclude sponsor or overlay logos from the stream.
- **No AI-generated content.** No AI voice, AI images or AI music. Real footage with
  cuts, crops and captions only.
- **No negative portrayal and no "hate farming".** Skip drama, rage-bait, misleading titles
  and fake narratives.
- **No view manipulation.** No bots, reposts, collab posts or duplicate uploads.
- Never build anything that evades Content ID or copyright detection (mirroring, speed
  tricks and so on). Never add VPN or location spoofing. Never auto-submit to campaign sites.

---

## 3. Project structure

```
youtube-shorts/
  CLAUDE.md                 # workflow instructions for future sessions (section 12)
  requirements.txt
  shorts.py                 # single CLI entry point (argparse subcommands)
  pipeline/
    __init__.py
    config.py               # paths, hardware detection, defaults
    download.py             # yt-dlp section download
    transcribe.py           # faster-whisper + loudness markers -> transcript.txt
    frames.py               # sample frames so Claude can see the stream layout
    captions.py             # ASS subtitle generation (word highlight + hook)
    render.py               # ffmpeg cut + 9:16 layout + burn captions
    upload.py               # YouTube Data API upload + scheduling
  secrets/                  # gitignored
    client_secret.json      # Google OAuth Desktop client (user downloads)
    token.json              # created by `shorts.py auth`
  music/                    # optional royalty-free tracks the user provides
  jobs/
    <campaign-slug>/
      job.json              # written by Claude from the pasted description
      src/source_0.mp4      # downloaded eligible section(s)
      src/source_0.wav      # 16 kHz mono audio
      words_0.json          # word-level timestamps (Claude must NOT read this)
      transcript.txt        # compact transcript Claude reads
      frames/source_0_*.jpg # layout reference frames
      clips.json            # written by Claude: moments + layout
      out/clip_01.mp4       # rendered shorts
      out/clip_01.txt       # title + description, ready to copy
      uploads.json          # upload state (resume-safe)
      links.txt             # YouTube Shorts URLs to submit to the campaign
```

Add a `.gitignore` that excludes: `.venv/`, `secrets/`, `jobs/*/src/`, `jobs/*/out/`,
`*.wav`, `*.mp4`.

---

## 4. One-time setup (do this with the user)

### 4.1 Tools (Windows)
```powershell
winget install --id Gyan.FFmpeg -e      # then restart the terminal so ffmpeg is on PATH
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```
`requirements.txt`:
```
yt-dlp
faster-whisper
numpy
tzdata
google-api-python-client
google-auth-oauthlib
google-auth-httplib2
```
- Use the venv python for every command: `.\.venv\Scripts\python.exe shorts.py ...`
- `shorts.py check` must verify that ffmpeg/ffprobe are on PATH, the packages import, and
  whether a CUDA GPU exists (`ctranslate2.get_cuda_device_count()`). It also checks
  whether `secrets/client_secret.json` and `token.json` exist. Print clear next steps.

### 4.2 Google / YouTube API (guide the user step by step)
1. https://console.cloud.google.com, then create a new project (for example "shorts-uploader").
2. Under **APIs & Services, Library**, enable **YouTube Data API v3**.
3. On the **OAuth consent screen**, choose External and fill in the app name and email. Add
   the user's own Google account under **Test users**.
4. Under **Credentials, Create credentials, OAuth client ID**, choose type **Desktop app**.
   Download the JSON and save it as `secrets/client_secret.json`.
5. Run `shorts.py auth`. A browser opens, the user logs into the **YouTube channel account**,
   and `secrets/token.json` is saved. Scope: `https://www.googleapis.com/auth/youtube.upload`.

Tell the user about these limits:
- **Private lock:** Uploads from unverified API projects (created after July 2020) are
  locked to **private** until the project passes Google's "YouTube API Services Audit".
  Until then the user must make each video public or scheduled in YouTube Studio. Give them
  the audit form link and explain it is optional but removes that manual step.
- **Weekly re-login:** While the consent screen is in "Testing" mode, refresh tokens expire
  after about 7 days, so `auth` must be rerun weekly. `upload` must detect an
  expired/invalid token and print "run `shorts.py auth`" instead of crashing.
- **Quota:** The default quota is 10,000 units/day. `videos.insert` costs about 1,600 units,
  so the limit is about **6 uploads/day**.

---

## 5. Data formats

### 5.1 `job.json` (Claude writes this from the pasted campaign description)
```json
{
  "name": "sypherpk-nov15",
  "campaign_summary": "Clip Sypher/Replays/Maddynf Nov 15 stream; $2 per 1K; min 10K views",
  "sources": [
    {"url": "https://www.twitch.tv/videos/123456789", "start": "00:45:00", "end": "03:10:00", "label": "Sypher POV"}
  ],
  "creators": ["Sypher", "Replays", "Maddynf"],
  "hashtags": ["#fortnitecreative", "#uefn"],
  "extra_tags": ["fortnite", "sypherpk", "replays", "maddynf", "shorts"],
  "rules": ["all 3 playing together must be the focus", "english only", "no logos", "..."],
  "max_clips": 30,
  "music": null,
  "upload": {
    "timezone": "America/New_York",
    "times": ["17:00", "19:00", "21:00"],
    "start_date": null,
    "privacy_fallback": "private"
  }
}
```
- `start`/`end` are optional. If they are missing, warn that the whole VOD will be downloaded.
- `music` is optional: `{"file": "music/track.mp3", "volume": 0.12}`. Default `null` (off).

### 5.2 `transcript.txt` (the only big file Claude reads)
This is compact by design so it costs few tokens. Merge whisper segments into lines of about
6 to 12 seconds. Times are **relative to the downloaded file** and include the source index.
Mark loud moments (hype, screaming, laughter) with `!`:
```
# source 0 | Sypher POV | file offset = VOD 00:45:00 | duration 02:25:00
[0 00:12:31-00:12:40] no no no he's right behind you turn around
[0 00:12:40-00:12:47] ! OH MY GOD REPLAYS WHAT WAS THAT
```
Loudness: decode `source_N.wav` and compute per-second RMS in dB with numpy. A line gets `!`
if its peak second is at or above the 90th percentile of the source.

### 5.3 `clips.json` (Claude writes this after reading the transcript and frames)
```json
{
  "layouts": {
    "0": {"mode": "split", "facecam": [0.70, 0.02, 0.28, 0.30], "game": null}
  },
  "clips": [
    {
      "id": 1,
      "source": 0,
      "start": "00:12:29.5",
      "end": "00:13:02.0",
      "title": "Replays pulled off the impossible 😭 #fortnite",
      "hook": "SYPHER COULDN'T BELIEVE THIS",
      "caption": "Replays clutched it with Sypher and Maddynf watching 😭 #fortnitecreative #uefn"
    }
  ]
}
```
- Box values are **fractions of the source frame**: `[x, y, w, h]`.
- `mode`:
  - `split`: facecam on top (1080x640) and gameplay below (1080x1280). This is the default
    when a facecam exists.
  - `blur`: the full frame is fit to 1080 wide and centered over a blurred, zoomed copy.
    Use it when there is no facecam.
  - `center`: plain center 9:16 crop.
- `game: null` means a center crop at the needed aspect. Otherwise it is a fractional box.

---

## 6. CLI commands

```
shorts.py check                          # environment check
shorts.py auth                           # one-time YouTube OAuth
shorts.py prepare jobs/<slug>            # download -> audio -> transcribe -> transcript.txt -> frames
shorts.py render  jobs/<slug> [--only 1,4,7] [--force]
shorts.py upload  jobs/<slug> [--dry-run] [--limit 6]
shorts.py status  jobs/<slug>            # what's downloaded/rendered/uploaded/scheduled
```
Every step must be **idempotent and resumable**: skip work that already exists unless
`--force` is passed. Log progress concisely. Never print huge outputs, because Claude reads
the logs and long output costs tokens.

---

## 7. Implementation details

### 7.1 Download (`download.py`)
- Run yt-dlp as a subprocess (more robust than the Python API):
  ```
  yt-dlp -f "bv*[height<=1080]+ba/b[height<=1080]" --merge-output-format mp4
         --download-sections "*00:45:00-03:10:00" -o jobs/<slug>/src/source_0.%(ext)s <url>
  ```
- yt-dlp supports Twitch VODs, YouTube and Kick. Handle these errors with friendly messages:
  sub-only VOD, deleted or expired VOD (Twitch deletes old VODs), age/region restriction.
- Extract the audio: `ffmpeg -i source_0.mp4 -ac 1 -ar 16000 source_0.wav`.

### 7.2 Transcribe (`transcribe.py`)
- faster-whisper with hardware auto-select:
  - CUDA GPU present: `large-v3-turbo` (or `medium.en`), `device="cuda"`, `compute_type="float16"`.
  - CPU only: `small.en`, `device="cpu"`, `compute_type="int8"`, `beam_size=1`.
  - Allow overriding with `--model`.
- Options: `language="en"`, `vad_filter=True`, `word_timestamps=True`,
  `condition_on_previous_text=False` (prevents repetition loops on long streams).
- Save `words_N.json` as a list of `{"w": "word", "s": 12.34, "e": 12.61}`.
- Build `transcript.txt` (5.2), print the estimated token count (characters / 4) and
  the duration.

### 7.3 Frames (`frames.py`)
- For each source, save 3 frames at 15%, 50% and 85% of the duration, scaled to 960 px
  wide: `frames/source_N_a.jpg` and so on.
- Claude views **one** frame per source (two if the layout is unclear) to set the
  `facecam` box and to spot logos or overlays to avoid.

### 7.4 Captions (`captions.py`): ASS file per clip
- `PlayResX: 1080`, `PlayResY: 1920`.
- Style **Caption**: Arial Black (falls back to Arial Bold), size about 80, white text, black
  outline 6, no shadow, alignment 5 (middle-center), ALL CAPS.
- Group words into chunks of up to 3 words. Start a new chunk on a pause of 0.6 s or more,
  or on sentence punctuation.
- One event per word: it shows the whole chunk with the **current word highlighted yellow**
  (`{\c&H00FFFF&}`; ASS colours are &HBBGGRR). Each event runs from the word's start to the
  next word's start, capped at the word end + 0.3 s.
- Shift all word times by `-clip_start`, and drop words outside the clip.
- Escape `{`, `}` and `\` in the words.
- Style **Hook**: white text in a black box (`BorderStyle: 3`), size about 64, shown for
  0 to 3.5 s.
- Positions:
  - `split`: hook at `\pos(540,640)` (the seam), captions at `\pos(540,1350)`.
  - `blur`/`center`: hook at `\pos(540,360)`, captions at `\pos(540,1450)`.

### 7.5 Render (`render.py`)
- Output: 1080x1920, 30 fps, H.264, `-crf 20 -preset medium -pix_fmt yuv420p`, AAC 160k,
  `-movflags +faststart`. Use `h264_nvenc` if available.
- Seek with `-ss <start> -t <dur> -i src` (input seeking, frame-accurate because we re-encode).
- **Windows path gotcha:** the `ass=` filter breaks on `C:\` paths. Run ffmpeg with
  `cwd=<job>/out` and refer to the ASS file by **relative name** (`ass=clip_01.ass`).
- `split` filter graph (boxes are fractions, so use `iw`/`ih` expressions):
  ```
  [0:v]crop=iw*W:ih*H:iw*X:ih*Y,scale=1080:640:force_original_aspect_ratio=increase,crop=1080:640[top];
  [0:v]crop=ih*1080/1280:ih:(iw-ih*1080/1280)/2:0,scale=1080:1280[bot];
  [top][bot]vstack=inputs=2,ass=clip_01.ass[v]
  ```
- `blur` filter graph:
  ```
  [0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2[bg];
  [0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,ass=clip_01.ass[v]
  ```
- Optional music (only if `job.music` is set). Keep it **quiet** so the streamer's voices stay clear:
  ```
  -stream_loop -1 -i music.mp3
  [1:a]volume=0.12[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]
  ```
- Also write `clip_NN.txt` containing the title, the description (caption, a blank line,
  hashtags, then "Credit: <creators>") and the tags.
- Validate each clip before rendering: 15 s ≤ duration ≤ 60 s, within source bounds, no
  overlap with other clips, title ≤ 100 chars, hook ≤ 6 words, the caption contains every
  required hashtag and at least one creator name. Fail loudly with a clear message.

### 7.6 Upload (`upload.py`)
- Resumable upload: `MediaFileUpload(path, chunksize=-1, resumable=True)`, then loop over `next_chunk()`.
- Body:
  - `snippet`: `title`, `description`, `tags`, `categoryId: "20"` (Gaming), `defaultLanguage: "en"`.
  - `status`: `privacyStatus: "private"`, `publishAt: <UTC ISO>`, `selfDeclaredMadeForKids: false`.
- Scheduling: walk forward through `upload.times` in `upload.timezone` (zoneinfo + tzdata),
  starting after the last scheduled slot in `uploads.json` (or now). Never schedule in the past.
- Save after **each** upload to `uploads.json`: `{clip_id: {video_id, publishAt, uploaded_at}}`.
  Never upload the same clip twice.
- On `quotaExceeded`: stop cleanly with "Daily YouTube quota reached, run `upload` again tomorrow".
- Append `https://youtube.com/shorts/<id>` to `links.txt`.
- `--dry-run` prints what would be uploaded and when, without calling the API.
- Note for the user: until the API audit passes, videos stay private regardless of
  `publishAt`. Print a reminder to make them public/scheduled in YouTube Studio.

---

## 8. Clip selection guide (for Claude, when writing `clips.json`)

- Read `job.json` rules first. Then read `transcript.txt`, plus one frame per source.
- Pick up to `max_clips` moments. Quality beats quantity: a clip under 10K views earns $0.
- Each clip:
  - **20 to 60 s**, ideally 25 to 45 s.
  - **Starts 1 to 2 s before the setup** (never mid-word or mid-sentence). The first 2 seconds
    must hook the viewer.
  - **Ends right after the payoff**: the kill, clutch, fail, laugh or punchline. No dead air at
    the end.
  - Prefer `!` (loud) lines: hype, screaming, laughing, big reactions.
  - Prefer moments where the campaign's required people are together and talking.
  - Must be understandable without context.
- Skip: dead time, menus or lobby waiting, sponsor reads, negative or drama moments, and
  anything that breaks the campaign rules.
- No overlapping clips and no near-duplicate moments.
- `title`: English, ≤ 60 chars, curiosity-driven, includes a creator name, honest (no clickbait lies).
- `hook`: ≤ 6 words, ALL CAPS, includes a creator name where natural.
- `caption`: 1 line, plus the required hashtags, plus a creator name.

---

## 9. Token budget rules (the user is on a Claude subscription)

- Claude must **never** read `words_*.json`, the raw yt-dlp/ffmpeg output, or video/audio files.
- Read `transcript.txt` once. If it is over about 60K tokens, read it in parts and keep notes
  of the candidate moments.
- View at most 1 or 2 frames per source.
- All scripts print short, summarised logs.
- Recommend one fresh Claude Code session per campaign (`/clear`).
- Rough cost: a 1-hour eligible section is about 15 to 20K tokens of transcript, plus about
  5K tokens of output, plus session overhead. Total roughly 50 to 100K tokens per campaign run.

---

## 10. Acceptance test

1. `shorts.py check` passes.
2. Create `jobs/test/job.json` using any short public **Creative Commons** YouTube video
   (search filter "Creative Commons"). Use a 5-minute section with `start`/`end`.
3. `prepare` produces `transcript.txt` and frames.
4. Claude writes `clips.json` with 2 clips (try `split` on one and `blur` on the other if possible).
5. `render` produces 1080x1920 mp4s with readable, in-sync word-highlighted captions and the
   hook in the first 3.5 s. Verify with `ffprobe` (resolution, duration). The user watches one.
6. `upload --dry-run` shows the correct titles, hashtags and US-time schedule.
7. Upload **one** test clip for real as private, then delete it in YouTube Studio.

---

## 11. Non-goals / never do

- No TikTok, Instagram or X upload. No auto-submission to Content Rewards / Whop.
- No AI voice, AI images or AI music generation.
- No logos, watermarks, mirroring or speed changes, and no other detection-evasion tricks.
- No downloading of content without campaign permission. The pipeline only processes
  URLs from a campaign description the user pastes.

---

## 12. Create this `CLAUDE.md` at the end

```markdown
# Shorts clipping pipeline

Use `.\.venv\Scripts\python.exe shorts.py ...` for every command. Full spec: SHORTS_PIPELINE_SPEC.md.

## When the user pastes a campaign description
1. Extract the VOD URLs + eligible timestamps, creators, required hashtags, rules, platforms
   and payout. If there are no VOD links/timestamps, ask the user for them. If YouTube is
   not an allowed platform, stop and tell the user.
2. Write `jobs/<slug>/job.json` (schema in spec 5.1). Show the user a 5-line summary.
3. Run `shorts.py prepare jobs/<slug>`. This is long, so run it in the background and wait.
4. View 1 frame per source and decide the layout. Read `transcript.txt` (never `words_*.json`).
5. Write `clips.json` following spec section 8 and the campaign rules.
6. Run `shorts.py render jobs/<slug>`. Tell the user the folder path so they can review the clips.
7. After the user says OK, run `shorts.py upload jobs/<slug>` (max ~6/day due to quota).
   Show `links.txt` and remind them: make the videos public in Studio if still private, and
   submit the links to the campaign page manually.

## Rules
- English only. Required hashtags + a creator name in every caption. No logos.
  No AI-generated media. No negative or hate-farm clips. Never evade copyright detection.
- Keep token usage low: compact logs, read the transcript once, 1 frame per source.
- If the token is expired, tell the user to run `shorts.py auth`.
```
