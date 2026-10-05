# YouTube Shorts automation

Finds what is trending on Shorts, judges every video with TypeSafe (System One / Jev), ranks them,
works out which *formats* you can copy as a faceless creator, then writes, voices, captions, renders
and (optionally) uploads a new Short in that style.

```
discover  ->  judge  ->  rank  ->  analyze  ->  produce  ->  upload
 yt-dlp      TypeSafe   weights   blueprints   Claude +      YouTube
                                               edge-tts +    Data API
                                               ffmpeg
```

## Setup

```bash
pip install -r requirements.txt
copy .env.example .env      # then fill in the keys
```

ffmpeg is needed for rendering. On Windows the tool can fetch a portable copy for you: click
**Install ffmpeg** on the console Overview, or run `python main.py setup-ffmpeg` (about 90 MB into
`data/bin/`). Otherwise install it yourself and, if it is not on PATH, set `FFMPEG_DIR` in `.env`. Keys:

| Key | Needed for | Where |
|---|---|---|
| `TYPESAFE_API_KEY` | judge, analyze, script QA | https://console.typesafe.ai/keys |
| `ANTHROPIC_API_KEY` **or** `CLAUDE_CODE_OAUTH_TOKEN` | produce (script writing) | API key from https://console.anthropic.com, or your Claude Pro/Max subscription: see below |
| `PEXELS_API_KEY` | optional stock footage backgrounds | https://www.pexels.com/api/ |
| `client_secrets.json` | upload | see below |

Without `TYPESAFE_API_KEY` the tool still discovers and ranks by view velocity and engagement,
but it cannot label formats, so `analyze` produces no blueprints and `produce` has nothing to work from.

### Using a Claude subscription instead of an API key

`produce` can write scripts through Claude Code's headless mode, which runs on a Claude Pro/Max
login rather than API billing. One-time setup in your own terminal (it opens a browser to sign in):

```bash
claude setup-token
```

Paste the token it prints into `.env` as `CLAUDE_CODE_OAUTH_TOKEN`. If `claude` is not on your PATH,
use the copy bundled with the Claude desktop app, for example
`%APPDATA%\Claude\claude-code\<version>\claude.exe setup-token`; the pipeline finds that copy on its own.
With `script_backend: auto` in `config.yaml` the API key is used when present, otherwise the subscription.

## Web console

```bash
python main.py web
```

Opens a local console at http://127.0.0.1:8787 with four tabs:

- **Overview**: which keys are configured, the latest run, the blueprints and the top ranked Shorts.
- **Pipeline**: run everything or one stage at a time, watch the live log, open any run's report.
- **Studio**: pick a blueprint and an optional angle, produce a Short, preview the video, read the
  script and its TypeSafe QA scores, upload to YouTube. Or paste / load your own script: by default it is
  **enhanced** (a scroll-stopping hook, tighter flow, a payoff and call to action, every fact and your
  point kept; TypeSafe checks hook, clarity, payoff, policy and faithfulness to your original and sends
  weak drafts back), or tick "as written" to voice it verbatim. Every produced
  Short has buttons to copy the title, the description with hashtags, or all three for a manual upload,
  plus a download link for the video.
- **Settings**: paste API keys (saved to `.env`, shown masked afterwards), upload
  `client_secrets.json`, and edit discovery hashtags, ranking weights, voice, backgrounds and privacy.

The console binds to localhost only because it can read and write your keys. Do not expose it.

## Commands

```bash
python main.py run                 # discover -> judge -> rank -> analyze, prints the report
python main.py run --produce       # ...and render a Short from the #1 blueprint into output/
python main.py produce --blueprint 2 --angle "why octopuses have three hearts"
python main.py produce --upload    # render then upload (private by default, see config.yaml)
python main.py produce --script-file my_script.txt --title "Why octopuses have three hearts" --hashtags "shorts,facts" --keywords "octopus underwater,ocean"
python main.py upload output/<dir> # upload something rendered earlier
python main.py rank --top 30       # re-rank after editing weights in config.yaml (no API calls)
```

Each `discover` creates `data/runs/<timestamp>/` with `shorts.json` (metadata, transcript, TypeSafe
judgment, signals, score), `analysis.json` and `analysis.md`. Later commands default to the latest run.

Discovery is two-stage and needs no API key. Stage 1 reads the Shorts shelf of every hashtag in
`config.yaml` (those skew toward all-time hits). Stage 2 takes the fastest-growing channels found in
stage 1 and pulls their newest Shorts, which is where the actually-trending material comes from.
Metadata and transcripts are cached in `data/cache/`, so re-runs are fast. YouTube search pages hide
Shorts from yt-dlp, so search is not used.

If the log shows "Sign in to confirm you're not a bot" or "HTTP Error 429", YouTube is rate-limiting
anonymous requests from this machine; the fetcher stops early after 8 failures in a row and keeps
whatever it has. Wait an hour, lower `discovery.workers`, raise `request_spacing_seconds`, or set
`cookies_from_browser: chrome` (or firefox/edge) so yt-dlp reuses your logged-in session. Roughly
300 metadata fetches per hour is a safe anonymous budget. The yt-dlp warning about a missing
JavaScript runtime (deno) is harmless here: we only read metadata, never download video.

## How the ranking works

Each Short is one TypeSafe request that asks nine questions in parallel (`shorts_pipeline/judge.py`):

- **Choice**: `format`, `topic`, `hook_style`
- **Score**: `hook_strength`, `replicable` (can a voiceover + stock footage pipeline remake this?), `evergreen`
- **Noul**: `english_ok`, `is_promo`, `clickbait_risk`

Code owns the policy. `rank.py` combines log-scaled views-per-hour and engagement with the three
normalised Scores using the weights in `config.yaml`, and drops Shorts whose `english_ok` or
`is_promo` Nouls fail the thresholds. Change a weight, run `rank` again; no re-judging.

`analyze.py` groups the survivors by (format, topic), sums their scores into an *opportunity*
number, throws out groups the pipeline cannot physically make (median replicability too low),
and picks each group's dominant hook style (probability-weighted). Those groups are the blueprints.
Groups whose replicability sits below `min_blueprint_replicable` (on-camera comedy, for example)
are listed after the faceless-friendly ones and tagged **[stretch]**: trending, but the originals
depend on a person or original footage, so expect a voiceover remake to underperform them.

## How production works

1. `script_gen.py` asks Claude (`claude-opus-5`, structured output) for a script that follows the
   blueprint and its exemplars' pacing, then asks TypeSafe to QA it (hook strength, clarity, format
   match, payoff, policy risk). Weak drafts are sent back with the reviewer notes, up to
   `script_max_attempts` times.
2. `tts.py` voices it with edge-tts and keeps per-word timings.
3. `captions.py` turns the timings into an ASS subtitle track with the spoken word highlighted.
4. `footage.py` finds footage that matches each line. Claude writes a literal, filmable search term per
   line; Pexels returns several portrait candidates, whose slugs and alt text describe them; TypeSafe
   picks the one that best illustrates the sentence, or says none does. Then it tries the video's
   subject-level term, then Pexels photos (with a slow zoom), and only then the generated background.
   The activity log says what was chosen for every line and why.

When Pexels has nothing that fits a line, an **AI-generated image** is made from the line's footage
term and the sentence itself, then shown with the same slow zoom. The default generator is
Pollinations (free, no key, about five seconds per image); with a key you can switch to Together AI
(FLUX), the Hugging Face Inference API (a free token gets a small monthly allowance; FLUX.1-schnell
by default, any text-to-image model id in the Model field) or OpenAI (gpt-image-1, paid per image) in
Settings, and "for every line" turns stock footage off entirely.
Images are cached under `data/cache/aiimg`. The free service throttles to roughly one image every
40 seconds after the first few, so "for every line" on a long script is slow; the keyed providers
are faster.

Caption looks are presets plus overrides (Settings -> Captions, with a live preview): bold Impact,
clean sans, boxed word, pop, minimal, neon; any font, size, colours, outline, highlight mode, position
and words per caption. Extra fonts go in `assets/fonts`.
5. `render.py` assembles everything with ffmpeg at 1080x1920 and mixes in background music: the
   track is looped to the video length, faded in and out, and ducked under the voice so words stay clear.

## Run it on a schedule

The console has a built-in scheduler (Settings -> Schedule). With it on, it spreads the configured
number of Shorts evenly across your active hours (default 20 a day between 06:00 and midnight, one
every 54 minutes), refreshes the trend analysis twice a day so the ranking reflects what is trending
now, and produces from a pool made of the top 12 trend blueprints of the latest run (the analysis keeps
20) plus your channel's own winners (see below): only those scoring at least 20% of the best adjusted
score, never stretch formats, picked in proportion to their score (so the leader gets the most videos
without taking the whole day). It avoids subjects used in recent videos and sends
every finished Short to Telegram. The Overview shows which blueprints are currently eligible and their
share. If the console was off, it runs the single most recent missed slot when it comes back,
never the whole backlog. Runs and their status are listed on the Overview.

The scheduler only runs while `python main.py web` is running. On Windows:

```bash
python main.py autostart install     # start the console at every logon (pythonw, no window)
python main.py autostart status
python main.py autostart remove
```

and set Windows power options so the machine does not sleep. `python main.py schedule` prints today's slots.

Settings changed in the console are written to `config.local.yaml` (untracked), so `git pull` never
conflicts with them; `config.yaml` holds the defaults.

## Clone a channel's style

Paste any channel into Studio ("Clone a channel's style") or run
`python main.py profile add https://www.youtube.com/@somechannel`. The tool reads the channel's
most-viewed Shorts with yt-dlp, classifies them with TypeSafe (same formats, topics and hook styles as
the trend ranking), and asks Claude to distil a style guide from the transcripts: voice and tone, hook
patterns, structure, pacing, recurring phrases, endings, dos and don'ts. Patterns only, never their
sentences. The profile is saved under `data/profiles/`.

Studio then lists every analysed Short of that channel (most viewed first, with its format x topic and
hook) and each row has "Produce like this": a remake of that one video, same subject and structure in
fresh words, in the channel's style (`produce --profile @somechannel --exemplar <video id>`; `profile
show @somechannel` prints the ids). "Produce in this style" (or `produce --profile @somechannel`)
instead picks one of the channel's top format x topic pairs by share with a new subject. Both use the
channel's typical length and the style guide, with the rest of the pipeline unchanged. A profile saved
without a style guide (Claude was unavailable) gets one on its next produce. Settings -> Schedule ->
Source lets the scheduler produce from a profile instead of the trend blueprints.

## Your channel's performance feeds back into the ranking

Add your OAuth `client_secrets.json` (Settings -> YouTube upload), or a YouTube Data API key (read-only:
Google Cloud console -> enable YouTube Data API v3 -> Credentials -> API key) plus your channel handle.
On every trend refresh, and on "Sync channel now", the tool reads every Short on your channel with its
public statistics and labels each one with the same TypeSafe judge used for trending Shorts (format,
topic, hook, from the video's own metadata and transcript; cached, so only new uploads cost anything).
Labelling the uploads themselves means the feedback covers videos produced on another machine, uploaded
by hand, or whose output folder is gone. It then computes each format x topic's, each format's and each
topic's median views per hour (over at most the first two weeks, so old videos are not punished for
having stopped growing; private uploads are ignored) relative to your channel's median. That ratio
(0.2-4, pulled toward neutral while only one or two videos support it; videos count after three hours)
is the channel factor.

Two things use it. The scheduler normalises the trend score, square-roots it so it cannot dominate, and
multiplies by the channel factor, so a format that trends globally but flops on your channel gets a
fraction of the videos. And every format x topic that beats your channel median by 30%+ becomes a
"channel winner" blueprint (up to 10, modelled on your best uploads of that pair) that joins the
production pool whether or not the trend run found anything like it, scored like a well-ranked trend
blueprint times its factor. Pairs you have never tried inherit their format's and topic's factors. The
Overview shows every eligible blueprint's weight, factor and basis (winners are marked with a star),
and Studio can produce a winner directly (`produce --blueprint-key "listicle_facts|space_astronomy"`).

The taxonomy has 35 formats and 49 topics (`judge.py`); changing it re-judges cached Shorts on the next
refresh.

```bash
python main.py channel sync
python main.py channel report
```

## Thumbnails

Every Short gets `thumbnail.jpg` (1080x1920): a frame from the first footage clip, a dark band, and a
3-6 word line Claude writes for it (your own scripts use the first words of the title), in the caption
style, plus your handle. It is the video poster in Studio with a download button, arrives in Telegram
as a photo after the description, and is set on YouTube automatically when the tool uploads (YouTube
only accepts custom thumbnails on phone-verified channels; otherwise the upload still succeeds).

## Intro and outro

Every Short gets a short generated title card at the start (the video title over a motion
background, 1.5 s) and an end card ("Follow for more" plus your handle, 2 s). Music runs across the
whole video; the voice and captions start after the intro. Text, seconds and on/off are in Settings
or `config.yaml -> production.intro / outro`. To use your own branded clips, put `assets/intro.mp4`
and `assets/outro.mp4` in the project and set `mode: clip`; clips are muted and the music plays over
them. `produce --no-intro --no-outro` skips them for one video.

## Telegram delivery

1. In Telegram, talk to **@BotFather**, create a bot, copy its token into Settings.
2. Send your new bot any message, then click **Find my chat ID** in Settings (or run
   `python main.py telegram discover`). To deliver into a group or channel, add the bot there first
   and send a message in it.
3. **Send test message** confirms the link. With "send automatically" on (the default), every finished
   Short arrives as a video with the title, description and hashtags as its caption, ready to copy into
   YouTube. Any card in Studio also has a **Send to Telegram** button, and there is
   `python main.py telegram send output/<dir>`.

Telegram bots can send videos up to 50 MB; a typical Short here is 3-6 MB.

## Background music

Tracks live in `assets/music/`. Fill it from the console's Settings tab (upload your own files, or
"Fetch free tracks", which searches Openverse for CC0 / CC-BY music from Freesound, Jamendo and
Wikimedia Commons), or from the command line:

```bash
python main.py music fetch --query "lofi chill" --count 5
python main.py music list
```

Only licences that allow commercial use and remixing are accepted (CC0, CC-BY, CC-BY-SA, public domain).
A CC-BY track gets its credit line appended to the video description automatically, so the "copy
description" buttons already include it. Pick a track per video in Studio (random, none, or a specific
file) or set the default, volume, fade and ducking under Settings.

## YouTube upload setup (one time)

1. https://console.cloud.google.com -> new project -> **APIs & Services -> Enable APIs** -> *YouTube Data API v3*.
2. **OAuth consent screen** -> External -> add your own Google account under *Test users*.
3. **Credentials -> Create credentials -> OAuth client ID -> Desktop app** -> download the JSON.
4. Save it as `client_secrets.json` in this folder.
5. The first `upload` opens a browser for consent; the token is cached in `data/youtube_token.json`.
6. **OAuth consent screen -> Publishing status -> Publish app** (In production). While the app is in
   *Testing*, Google expires its tokens after 7 days and every upload and channel sync fails with
   "Token has been expired or revoked" until you consent again. Publishing needs no verification for
   your own channel; Google only shows an "unverified app" warning on the consent page. When a token
   does expire, the next upload or "Sync channel now" simply opens the consent page again.

Uploads default to **private** so you can review before publishing: they appear in YouTube Studio
under Content with the Private visibility, not on your public channel. Publish from the Studio card
("Make public" / "Unlisted") or with `python main.py publish output/<dir>`, or set the upload privacy
to public in Settings to skip the review step. Unverified Google Cloud projects have a daily upload quota
of roughly six videos.

## Tuning

- `config.yaml -> discovery.hashtags / search_queries`: steer discovery toward a niche.
- `ranking.weights`: how much velocity vs. replicability vs. hook matters to you.
- `production.voice`: `edge-tts --list-voices` shows every option.
- `production.target_seconds`: 30-55 s is the Shorts sweet spot.
