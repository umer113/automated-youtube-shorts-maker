# Shorts clipping pipeline

Use `.\.venv\Scripts\python.exe shorts.py ...` for every command. Full spec: SHORTS_PIPELINE_SPEC.md.
The user writes in Hinglish/Urdu (Roman). Reply the same way: short bullet points.

## Web UI (main way the user works)
- `start.bat` -> http://localhost:8790 . Paste campaign, choose clip count + upload mode, watch progress, copy links.
- Code: `web/server.py` (FastAPI), `web/index.html`, `pipeline/runner.py` (stages + ETA + state.json),
  `pipeline/llm.py` (headless `claude -p` with a short system prompt, Read tool only = low token use).
- No upload cap in code; uploads stop only when YouTube refuses (quota / channel daily limit).
  Errors / limits -> "Resume" button re-runs the job; finished steps are skipped.
- "Make more clips" on a finished job = new batch job (`parent`), same campaign; `plan_sources` skips
  footage listed in job.json `used` (earlier batches' windows), so clips never repeat.

## When the user pastes a campaign description (in chat instead of the UI)
1. Extract the VOD URLs + eligible timestamps, creators, required hashtags, rules, platforms
   and payout. If there are no VOD links/timestamps, ask the user for them. If YouTube is
   not an allowed platform, stop and tell the user.
2. Write `jobs/<slug>/job.json` (schema in spec 5.1). Show the user a 5-line summary.
3. Run `shorts.py prepare jobs/<slug>` in the background (it is long) and wait for the notification.
4. View 1 frame per source (`jobs/<slug>/frames/`) and decide the layout (split + facecam box,
   or blur). Read `transcript.txt` once. NEVER read `words_*.json`.
5. Write `clips.json` following spec section 8 and the campaign rules.
6. Run `shorts.py render jobs/<slug>` (in the background if many clips). Give the user the
   `jobs/<slug>/out` folder path to review.
7. After the user says OK: `shorts.py upload jobs/<slug>` (no cap; stops if YouTube refuses). Show
   `links.txt`. Remind them: set videos Public/Scheduled in Studio if still private, and
   submit the links to the campaign page manually.

## Layout (user preference: look like normal full-screen Shorts)
- Default `"mode": "center"` = full-screen 9:16 crop. Set `"x"` (0..1) to aim the crop at the action/speaker.
- Stream with facecam: `"split"` (facecam top, gameplay bottom), also full-screen.
- NEVER use `"blur"` (blurred bars) unless the user explicitly asks.

## Campaign overlay / required caption text (job.json)
- `"overlay": {"file": "assets/<name>.png", "width": 760, "y": 170}`: transparent PNG burned on top for the whole clip (e.g. KreekCraft logo). Crop to the alpha bbox first. "Make more clips" inherits it from the parent job.
- `"caption_line": "Watch X on YouTube and Twitch"`: appended to every caption/description (parse_campaign fills it when the rules require one).
- Campaigns need links submitted within 30 min of going live: use "Upload right away" mode; "Re-post all" re-makes + re-uploads a job.

## Lean mode (default, `"lean": true` in job.json)
- `prepare` downloads AUDIO ONLY (exact cut) + 2 tiny video samples for frames.
- `render` downloads only each chosen clip's range, renders, deletes the piece.
- `upload` deletes each mp4 after it is on YouTube. `shorts.py clean jobs/<slug>` frees the rest.
- `upload --private` = private, no schedule (use for tests). `upload --dry-run` = preview only.
- Login: `shorts.py auth` opens Chrome profile "Profile 20" (shaikh).

## Rules
- English only. Required hashtags + a creator name in every caption. No logos.
  No AI-generated media. No negative or hate-farm clips. Never evade copyright detection.
- Keep token usage low: compact logs, read the transcript once, 1 frame per source.
- If the token is expired, tell the user to run `shorts.py auth` (opens a browser).
