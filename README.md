# VoiceFlow

A speech aid for people who can't rely on their voice. Clone your voice from a
short recording, then type and have it spoken out loud in that voice. Open a
live room and the person you're talking to just listens — no chat window to
watch, nothing to read.

Built for situations like a vocal cord dysfunction flare-up, or Tourette's
making sustained speech unpredictable.

## Requirements

- Python 3.9+
- An [ElevenLabs](https://elevenlabs.io) account. Voice cloning needs Starter
  tier or higher; the premade voices work on free.

## Running it locally

```bash
pip install -r requirements.txt
```

```bash
python main.py
```

Then open <http://127.0.0.1:8000>. Paste your ElevenLabs key into the connection
button at the top of the page.

`python main.py` binds to loopback only. For auto-reload while developing:

```bash
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

### Microphone access needs a secure context

Browsers only grant `getUserMedia` over HTTPS or on `localhost`. If you serve
this over plain HTTP on any other host, the record button will fail silently and
only file upload will work. Terminate TLS in front of the app.

## Where your API key lives

| Situation | What happens |
|---|---|
| You, on the machine running the server | Key is kept in your browser's `localStorage`. You can also tick a box to write it to `credentials.json` so this machine stops asking. |
| Anyone else (another device, the internet) | Key is kept in that browser only, sent with each request, used once, never written to the server. |

A key on the server's disk is **only** used for requests that come from the
server's own machine. Remote visitors always supply their own — so a deployed
instance can't quietly spend your credits or touch your cloned voices.

`credentials.json` is gitignored, and stored in plaintext. On Windows the
`chmod 0600` the app attempts is a no-op, so treat that file as you would any
other secret on disk. Copy `credentials.example.json` if you'd rather set it up
by hand.

## Hosting it for other people

Set these before anything else:

```bash
VOICEFLOW_PUBLIC=1
VOICEFLOW_BEHIND_PROXY=1   # if nginx / Caddy / Cloudflare / a PaaS router is in front
```

`VOICEFLOW_PUBLIC=1` disables writing keys to disk and disables server-side key
use entirely. `VOICEFLOW_BEHIND_PROXY=1` makes the app read client IPs from
`X-Forwarded-For` and stops it from ever mistaking a proxied visitor for a local
one — **without it, a proxy that doesn't set `X-Forwarded-For` makes every
visitor look like they're sitting at your keyboard.**

See `.env.example` for the full list of settings.

### Run exactly one worker

Rooms live in process memory. With `--workers 2` (or any autoscaled deployment)
a room created in one worker is invisible to the WebSocket that lands in
another, and every listener gets "room not found". Either pin it to one worker,
or move `rooms` into Redis first.

## Live rooms

The host opens a room and shares an 8-character code or a link. Listeners keep
the tab open in the background; audio plays automatically as the host types.

- Rooms expire 6 hours after creation and are swept every minute.
- Max 500 rooms, 3 per IP, 25 listeners per room.
- The room code is the only thing guarding the conversation, so
  `/api/rooms/{code}/exists` and listener connections are rate limited to make
  guessing impractical. Treat a room link like a phone number: don't post it
  publicly.

Because audio and transcripts pass through the server's memory on the way to
listeners, host this somewhere you'd be comfortable having a private
conversation. Nothing is written to disk, and nothing is retained after a room
closes.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/status` | Validate the current key |
| `GET` | `/api/key-status` | What this instance allows |
| `POST`/`DELETE` | `/api/key/save` | Local requests only, non-public mode only |
| `POST` | `/api/clone-voice` | Multipart, 25 MB cap |
| `GET` | `/api/voices` | List voices |
| `POST` | `/api/tts` | 2500 character cap |
| `DELETE` | `/api/voices/{id}` | Delete a cloned voice |
| `POST` | `/api/rooms/create` | Returns code + host token |
| `GET` | `/api/rooms/{code}/exists` | Rate limited |
| `DELETE` | `/api/rooms/{code}` | Host token in `X-Host-Token` |
| `WS` | `/ws/host/{code}` | Auth via first frame: `{"type":"auth","token":…}` |
| `WS` | `/ws/listen/{code}` | Receives transcript + audio |

Rate limits are skipped entirely for requests from the server's own machine, so
your own use is never throttled.
