# Voice studio API

The studio (`tools/halo_voice/studio/`) is where new multiplayer announcer
lines are made: each line's takes (Qwen3-TTS clones of the announcer),
their QA scores, the pick, ratings, comments, and tests in the game. The
page at `/` and this API do the same things; everything is JSON over HTTP,
on the tailnet only (`http://100.83.137.21:8765`).

Every write is `POST` with a JSON body, and records who did it: `"by":
"user"` or `"by": "claude"` (default `"claude"`; the page sends `"user"`).
Errors are `{"error": "..."}` with a 4xx/5xx status.

```sh
S=http://100.83.137.21:8765
curl -s $S/api/activity?since=2026-10-05T00:00:00Z     # catch up
curl -s $S/api/lines | jq '.lines[] | {name, text, pick}'
curl -s -XPOST $S/api/lines/overtime/comments -d '{"text": "Take 3 has more punch.", "by": "claude"}'
```

## Reading

| | |
|---|---|
| `GET /api/state` | Everything the page shows: `lines` (with archived ones), the last 40 `jobs`, the `worker`, the last 40 `activity` entries, and the state's `version`. `?wait=V&timeout=25` waits until the version passes V (a long poll). |
| `GET /api/lines` | The lines (`?archived=1` with the archived ones). |
| `GET /api/lines/<name>` | One line. |
| `GET /api/originals` | The announcer's 36 clips (`group`: calm, hype or other: the reference montages' styles), with their Whisper transcripts and voice scores, and the two reference montages. |
| `GET /api/jobs` | Jobs, newest first (`?status=queued,running`, `?limit=50`). |
| `GET /api/jobs/<id>` | One job and its log's last lines (`?tail=60`). |
| `GET /api/activity` | What everyone did, oldest first: `?since=` an ISO time or Unix seconds, `?since_id=` an entry id, `?by=user` (or `claude`, `worker`, comma-separated), `?line=`, `?limit=` (200). Returns `now` and `last_id` to continue from. |

A line: `name` (the game's name for it, `[a-z0-9_]{1,31}`), `text`,
`style` (`calm` or `hype`: which reference montage the model imitates),
`archived`, `mastering` (`gain_db`, `eq`, `stereo`), `pick` (`{take, by,
at}` or null), `qa_best` (QA's best take), `takes`, `comments` (`{id, at,
by, text, reply_to}`), `ingame` (the last in-game tests), `jobs` (queued or
running job ids).

A take: `n`, `seed` and `salt` (generate_halo.py's), the `text` and
`style` it was made with (`stale`: they are not the line's now), `master`
(`gain_db`, `peak_dbfs`, `limited_samples`, `eq`, `stereo`, `trim_db`),
`qa` (`heard`: what Whisper heard, `cer`/`wer`: character and word error
rates against the text, `similarity`: the cosine of its speaker embedding
with the clips'; the clips score 0.77 to 0.92 against each other, median
0.85), `ratings` (`{"user": {rating, note, at}, "claude": {...}}`),
`picked`, `best`, `mastered_url` and `raw_url` (WAVs).

An in-game test: `take`, `url` (the clip of the game's mix, 48 kHz),
`clip_start` (where it starts in the capture), `line` and `stock` (where
the line and the announcer's own "Slayer." are in the capture: `at`
seconds, `correlation`, `gain_db`, `speech_dbfs` in the mix), `heard`.

## Writing

| | body |
|---|---|
| `POST /api/lines` | `{name, text, style, generate?: N}`: a new line (N > 0 queues N takes). |
| `POST /api/lines/<name>/edit` | `{text?, style?}`. Takes keep the text they were made with (`stale`). |
| `POST /api/lines/<name>/pick` | `{take: N, note?}`. |
| `POST /api/lines/<name>/takes/<n>/rate` | `{rating: "good" \| "bad" \| null, note?}`: one rating per person per take, replaced each time. |
| `POST /api/lines/<name>/comments` | `{text, reply_to?: comment id}`. |
| `POST /api/lines/<name>/comments/<id>/delete` | One's own comment (and its replies). |
| `POST /api/lines/<name>/generate` | `{count?: 1-8 (2), salt?}`: new takes, numbered after the last (each number its own seed; a salt changes every seed). The job then masters and checks the line. |
| `POST /api/lines/<name>/mastering` | `{gain_db?: -12..6, eq?: bool, stereo?: bool, remaster?: bool}`: the line's mastering; a change (or `remaster: true`) re-masters its takes and checks them again, without generating. |
| `POST /api/lines/<name>/master` | Re-master and check with the current options. |
| `POST /api/lines/<name>/qa` | Check the takes again. |
| `POST /api/lines/<name>/ingame` | `{take?: N (the pick), seconds?: 16-60 (24)}`: the in-game test. |
| `POST /api/lines/<name>/archive`, `/unarchive` | Hide a line from the job (its takes are kept). |
| `POST /api/lines/<name>/delete` | An archived line, with its audio, for good. |
| `POST /api/install` | `{lines?: [names]}`: every (or these) line's pick into the game's test data folder (`~/halo-voice/run/voice`, with `voice.json`). Synchronous. |
| `POST /api/jobs/<id>/cancel` | A queued or running job. |
| `POST /api/jobs/<id>/retry` | A failed or cancelled job, queued again. |

## Jobs

One at a time (the GPU is shared), in order. `kind`: `generate` (then
master and qa), `master` (then qa), `qa`, `ingame`. `status`: `queued`,
`running` (`stage` says which step), `done`, `failed` (`error`),
`cancelled`. A line with no pick gets QA's best take. A job running when
the studio stops is queued again when it starts.

- generate: Qwen3-TTS (`blitz-roster-audio:qwen3-tts`, GPU),
  `generate_halo.py --only <line> --take-numbers ...`; loading the model
  takes about a minute, then a few seconds a take.
- master, qa: `halo-voice-tools` (CPU), `halo_voice.py master/qa --only`.
- ingame: `~/halo-voice/run_test.sh` (a local Slayer game on Blood Gulch,
  headless, the line 10 s in, only that take in its voice folder), then
  `halo_voice.py locate`; the clip runs from a second before the
  announcer's "Slayer." to 1.5 s after the line.

## Running it

On spark, as a systemd user service (the user lingers, so it outlives
logins and comes back after a reboot):

```sh
tools/halo_voice/studio/studio.sh deploy spark    # from a checkout: copy and (re)start
ssh spark 'sh ~/halo-voice-studio/tools/halo_voice/studio/studio.sh logs'   # or start, stop, restart, status
```

State: `~/model-workloads/halo-voice/studio/` (`studio.json`, `job/` the
tools' job folder, `jobs/<id>.log`, `game/` in-game clips). It was started
from `~/model-workloads/halo-voice/job/`, which it leaves as it was.
