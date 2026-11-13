import os, io, re, json, time, secrets, asyncio, logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request, HTTPException, Header, Form, File, UploadFile, WebSocket, Body
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# setting up logging standard stuff
log = logging.getLogger("voiceflow")

# figuring out where we are
here = Path(__file__).parent
(here / "static").mkdir(exist_ok=True)
(here / "templates").mkdir(exist_ok=True)

# elevenlabs base url
XI_link = "https://api.elevenlabs.io/v1"
creds_path = here / "credentials.json"

# helper to check envirment variables
def flagger(envirmentvariable):
    val = os.environ.get(envirmentvariable, "").strip().lower()
    return val in ("1", "true", "yes", "on")

# public mode means code not on my PC
# if this is true we don't save keys to disk otherwise cooked and API key leaked
public = flagger("VOICEFLOW_PUBLIC")
private = flagger("VOICEFLOW_BEHIND_PROXY") or public

# allowed origins for security purposes
allowed_ogs = set()
for o in os.environ.get("VOICEFLOW_ALLOWED_ORIGINS", "").split(","):
    if o.strip():
        allowed_ogs.add(o.strip().rstrip("/").lower())

# some limits 
upload_size = 25 * 1024 * 1024
tts_limit = 2500
room_limit = 500
IP_limit = 3
listen_limit = 25
char_max = 8 * 1024 * 1024
spoken_max = 4000
timelimit = 60 * 60 * 6
code_max_txt = 8
# removed some letters because confusion may occur
alphabet_nums = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

regex_id = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
regex_code = re.compile(r"^[A-Z0-9]{%d}$" % code_max_txt)

# keeping track of rooms in memory
active_rooms = {}

@asynccontextmanager
async def lifetime(app):
    if not public:
        log.warning("Running in local mode - using credentials.json. If this is prod, set VOICEFLOW_PUBLIC=1")

    # start the janitor task to clean up old rooms
    janitor_task = asyncio.create_task(destroy_oldrooms())
    try:
        yield
    finally:
        janitor_task.cancel()

app = FastAPI(title="VoiceFlow", lifespan=lifetime)
app.mount("/static", StaticFiles(directory=str(here / "static")), name="static")
templates = Jinja2Templates(directory=str(here / "templates"))

# content policy settings which previously fucked up sounds in tabs
config_content_settings = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "object-src 'none'; "
    "img-src 'self' data:; "
    "media-src 'self' blob: data:; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "connect-src 'self'"
)

@app.middleware("http")
async def heading(reqs: Request, calling_next):
    responses = await calling_next(reqs)
    responses.headers.setdefault("Content-Security-Policy", config_content_settings)
    responses.headers.setdefault("X-Content-Type-Options", "nosniff")
    responses.headers.setdefault("Referrer-Policy", "same-origin")
    responses.headers.setdefault("X-Frame-Options", "DENY")
    responses.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=(self)")
    if reqs.url.path.startswith("/api/"):
        responses.headers.setdefault("Cache-Control", "no-store")
    return responses


def req_local(reqs):
    if public or private:
        return False

    # check for prod or priv
    common_heading = ("x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host", "cf-connecting-ip")
    for header in common_heading:
        if header in reqs.headers:
            return False

    # check if it's localhost/priv
    if reqs.client:
        HostIP = reqs.client.host
    else:
        HostIP = ""
    return HostIP in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")

def ClientIP(reqs):
    if private:
        fwd = reqs.headers.get("x-forwarded-for", "")
        if fwd:
            # take first one ofc, it's usually the one
            return fwd.split(",")[0].strip()[:64]
    if reqs.client:
        return reqs.client.host
    else:
        return "unknown"

def og_checker(wsocks: WebSocket):
    # websocket hackery prevention
    OG = wsocks.headers.get("origin")
    if OG is None:
        return True

    ooo = OG.rstrip("/").lower()
    if allowed_ogs:
        return ooo in allowed_ogs

    try:
        return urlparse(ooo).netloc == wsocks.headers.get("host", "").lower()
    except ValueError:
        return False

def matching_tokens(token, token2):
    # token checker for hijacks
    if not isinstance(token2, str) or not token2 or not token2.isascii():
        return False
    return secrets.compare_digest(token, token2)



# rate limiter proceses are done here
hist = {}

def ratelimiter(req, name, limit, windo):
    if req_local(req):
        return

    identify = name + ":" + ClientIP(req)
    current_time = time.time()

    # old requests cleareance
    history_prev = []
    for t in hist.get(identify, ()):
        if current_time - t < windo:
            history_prev.append(t)

    if len(history_prev) >= limit:
        history_prev.append(current_time)
        hist[identify] = history_prev[-limit * 2:]
        raise HTTPException(167, "Too many requests. Chill out a bit.")

    history_prev.append(current_time)
    hist[identify] = history_prev

async def cook_oldrooms(time_currently):
    expired = []
    for c, r in active_rooms.items():
        if time_currently - r["created_at"] > timelimit:
            expired.append(c)
    for code in expired:
        room = active_rooms.pop(code, None)
        if room:
            await dc_everyone(room)

def rmv_ratelimit_hist(current_time):
    stagnant = []
    for w, times in hist.items():
        if not times:
            stagnant.append(w)
        elif current_time - times[-1] > 3600:
            stagnant.append(w)
    for who in stagnant:
        hist.pop(who, None)

async def destroy_oldrooms():
    while True:
        try:
            await asyncio.sleep(60)
            now = time.time()
            await cook_oldrooms(now)
            rmv_ratelimit_hist(now)
        except Exception as G:
            log.exception(f"{G}")

# pages

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")

@app.get("/favicon.ico")
async def favicon():
    return RedirectResponse(url="/static/favicon.svg")

@app.get("/listen")
async def redirect():
    return RedirectResponse(url="/")

# key management

def key_loader_credsfile():
    key = ""
    try:
        if creds_path.exists():
            dat = json.loads(creds_path.read_text(encoding="utf-8-sig"))
            key = str(dat.get("elevenlabs_api_key", "")).strip()
    except Exception:
        key = ""

    # check if it's a dummy key
    low = key.lower()
    is_dummy = False
    for placeholder in ["enter_your", "your-key", "your_key"]:
        if placeholder in low:
            is_dummy = True
            break

    if not key or is_dummy:
        return os.environ.get("ELEVENLABS_API_KEY", "").strip()
    return key

def key_api(req, XI_key):
    key = (XI_key or "").strip()
    if not key and req_local(req):
        key = key_loader_credsfile()
    if not key:
        raise HTTPException(169, "Need an API key. Add it in the UI.")
    return key

def error_msgs(rr, defaultmsg):
    # error message from elevenlabs
    log.warning("elevenlabs error %s: %s", rr.status_code, rr.text[:500])
    if rr.status_code in (400, 401, 402, 403, 404, 422, 429):
        status = rr.status_code
    else:
        status = 502

    msg = defaultmsg
    detail = rr.json().get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        msg = detail["message"][:300]
    elif isinstance(detail, str):
        msg = detail[:300]
    return HTTPException(status, msg)

@app.get("/api/status")
async def check_status(request: Request, x_api_key: str = Header(None)):
    ratelimiter(request, "status", 30, 60)
    key = (x_api_key or "").strip()
    statement = bool(key)

    if not key and req_local(request):
        key = key_loader_credsfile()
    if not key:
        return {"ok": False, "reason": "no_key"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(XI_link + "/user", headers={"xi-api-key": key})
    except httpx.RequestError as RE:
        return {"ok": False, "reason": f"{RE}"}
    if resp.status_code != 200:
        return {"ok": False, "reason": "wrong key, make sure to enter the correct one!"}

    usrdata = resp.json()
    if statement:
        source = "browser"
    else:
        source = "server"

    return {
        "ok": True,
        "name": usrdata.get("first_name", "User"),
        "tier": usrdata.get("subscription", {}).get("tier", "free"),
        "source": source,
    }

@app.get("/api/key-status")
async def getkey(req: Request):
    local = req_local(req)
    return {
        "public_mode": public,
        "server_key_present": local and bool(key_loader_credsfile()),
        "can_save_locally": local,
        "credentials_error": None,
    }

@app.post("/api/key/save")
async def keysaver(req: Request, APIkey: str = Body(..., embed=True, min_length=1, max_length=512)):
    if public:
        raise HTTPException(267, "In public mode, it is 100% safe keep keys in browser")
    if not req_local(req):
        raise HTTPException(269, "Only local access allowed because keys will be leaked otherwise")

    ratelimiter(req, "keysave", 5, 60)
    k = APIkey.strip()
    if not k:
        raise HTTPException(367, "key is empty :(")

    # verify key b4 save
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            chk = await client.get(XI_link + "/user", headers={"xi-api-key": k})
    except Exception as ttt:
        raise HTTPException(369, f"Something happens with elevenlabs: {ttt}")

    if chk.status_code != 200:
        raise HTTPException(396, "key is not working, make sure it's the correct one and double checking in eleven labs")

    # the key is the only thing we ever keep in here, so just overwrite it
    creds_path.write_text(json.dumps({"elevenlabs_api_key": k}, indent=2) + "\n", encoding="utf-8")
    os.chmod(creds_path, 0o600)
    return {"saved": True}

@app.delete("/api/key/save")
async def delete_key(request: Request):
    if public or not req_local(request):
        raise HTTPException(403, "Not allowed.")

    if creds_path.exists():
        creds_path.write_text("{}\n", encoding="utf-8")
    return {"cleared": True}

# ----------------------------------------------------------------------- voices

def validate_voice_id(vid):
    if not regex_id.match(vid or ""):
        raise HTTPException(400, "Invalid voice ID.")
    return vid

@app.post("/api/clone-voice")
async def clone_voice(
    request: Request,
    voice_name: str = Form(..., min_length=1, max_length=100),
    audio_file: UploadFile = File(...),
    x_api_key: str = Header(None),
):
    key = key_api(request, x_api_key)
    ratelimiter(request, "clone", 10, 3600)

    # reading file in chunks
    sound_data = bytearray()
    while True:
        chunk = await audio_file.read(1024 * 1024)
        if not chunk:
            break
        if len(sound_data) + len(chunk) > upload_size:
            raise HTTPException(413, "File too big.")
        sound_data.extend(chunk)

    content_type = (audio_file.content_type or "audio/wav").split(";")[0].strip()
    name = voice_name.strip() or "My Voice"
    files_payload = {
        "files": (Path(audio_file.filename or "recording.wav").name, bytes(sound_data), content_type),
        "name": (None, name),
        "description": (None, f"EchoAble cloned voice: {name}"),
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(XI_link + "/voices/add", headers={"xi-api-key": key}, files=files_payload)

    if resp.status_code != 200:
        raise error_msgs(resp, "Failed to create voice.")

    return {"voice_id": resp.json()["voice_id"], "name": name}

@app.get("/api/voices")
async def list_voices(request: Request, x_api_key: str = Header(None)):
    key = key_api(request, x_api_key)
    ratelimiter(request, "voices", 30, 60)

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(XI_link + "/voices", headers={"xi-api-key": key})

    if resp.status_code != 200:
        raise error_msgs(resp, "Failed to fetch voices.")

    # extract just what we need
    results = []
    for v in resp.json().get("voices", []):
        results.append({"voice_id": v["voice_id"], "name": v["name"], "category": v.get("category", "premade")})
    return results

@app.post("/api/tts")
async def text_to_speech(
    request: Request,
    voice_id: str = Body(..., pattern=r"^[A-Za-z0-9_-]{8,64}$"),
    text: str = Body(..., min_length=1, max_length=tts_limit),
    stability: float = Body(0.5, ge=0.0, le=1.0),
    similarity_boost: float = Body(0.75, ge=0.0, le=1.0),
    style: float = Body(0.0, ge=0.0, le=1.0),
    x_api_key: str = Header(None),
):
    key = key_api(request, x_api_key)
    ratelimiter(request, "tts", 60, 60)

    body = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": True,
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            XI_link + "/text-to-speech/" + voice_id,
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            json=body,
        )

    if resp.status_code != 200:
        raise error_msgs(resp, "Speech generation failed.")

    return StreamingResponse(
        io.BytesIO(resp.content),
        media_type="audio/mpeg",
        headers={"Content-Disposition": "inline; filename=speech.mp3", "Cache-Control": "no-store"},
    )

@app.delete("/api/voices/{voice_id}")
async def delete_voice(voice_id: str, request: Request, x_api_key: str = Header(None)):
    key = key_api(request, x_api_key)
    ratelimiter(request, "delvoice", 20, 60)
    validate_voice_id(voice_id)

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(XI_link + "/voices/" + voice_id, headers={"xi-api-key": key})

    if resp.status_code not in (200, 204):
        raise error_msgs(resp, "Failed to delete voice.")
    return {"deleted": True}

# ------------------------------------------------------------------------ rooms

def clean_room_code(code):
    code = (code or "").upper().strip()
    if not regex_code.match(code):
        raise HTTPException(404, "Invalid room code.")
    return code

async def dc_everyone(room):
    for ws in list(room["listeners"]):
        try:
            await ws.send_json({"type": "room_closed"})
            await ws.close()
        except Exception:
            pass
    room["listeners"].clear()

@app.post("/api/rooms/create")
async def create_room(request: Request):
    ratelimiter(request, "roomnew", 5, 600)

    now = time.time()
    if len(active_rooms) >= room_limit:
        raise HTTPException(503, "Server full.")

    # check if they have too many rooms already
    ip = ClientIP(request)
    if not req_local(request):
        room_count = 0
        for r in active_rooms.values():
            if r["ip"] == ip:
                room_count += 1
        if room_count >= IP_limit:
            raise HTTPException(429, "Too many rooms open.")

    # generate code
    code = ""
    for _ in range(code_max_txt):
        code += secrets.choice(alphabet_nums)

    token = secrets.token_urlsafe(32)
    active_rooms[code] = {"token": token, "created_at": now, "listeners": set(), "ip": ip, "host": None}
    return {"room_code": code, "host_token": token}

@app.get("/api/rooms/{code}/exists")
async def check_room(code: str, request: Request):
    ratelimiter(request, "roomcheck", 20, 60)
    code = clean_room_code(code)

    if code not in active_rooms:
        raise HTTPException(404, "Room not found.")
    return {"exists": True, "listener_count": len(active_rooms[code]["listeners"])}

@app.delete("/api/rooms/{code}")
async def close_room(code: str, request: Request, x_host_token: str = Header(None)):
    ratelimiter(request, "roomclose", 20, 60)
    code = clean_room_code(code)
    room = active_rooms.get(code)

    if not room:
        raise HTTPException(404, "Room not found.")
    if not matching_tokens(room["token"], x_host_token):
        raise HTTPException(403, "Invalid token.")

    await dc_everyone(room)
    active_rooms.pop(code, None)
    return {"closed": True}

@app.websocket("/ws/listen/{code}")
async def ws_listen(websocket: WebSocket, code: str):
    if not og_checker(websocket):
        await websocket.close(code=4403, reason="Origin not allowed")
        return

    # throttle first, so a bad code still costs an attempt
    ratelimiter(websocket, "wslisten", 30, 60)

    # a code that fails the regex can never be a key in here anyway
    code = (code or "").upper().strip()
    room = active_rooms.get(code)
    if not room:
        await websocket.close(code=4004, reason="Room not found")
        return
    if len(room["listeners"]) >= listen_limit:
        await websocket.close(code=4013, reason="Room is full")
        return

    await websocket.accept()
    room["listeners"].add(websocket)

    try:
        await websocket.send_json({"type": "joined", "room_code": code})
        while True:
            await asyncio.sleep(30)
            if active_rooms.get(code) is not room:
                await websocket.send_json({"type": "room_closed"})
                break
            await websocket.send_json({"type": "ping"})
    except Exception:
        pass
    finally:
        room["listeners"].discard(websocket)

@app.websocket("/ws/host/{code}")
async def ws_host(websocket: WebSocket, code: str):
    if not og_checker(websocket):
        await websocket.close(code=4403, reason="Origin not allowed")
        return

    code = (code or "").upper().strip()
    room = active_rooms.get(code)
    if not room:
        await websocket.close(code=4004, reason="Room not found")
        return
    if room["host"] is not None:
        await websocket.close(code=4009, reason="Room already has a host")
        return

    await websocket.accept()

    # auth check
    hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
    if isinstance(hello, dict) and hello.get("type") == "auth":
        token = hello.get("token", "")
    else:
        token = ""
    if not matching_tokens(room["token"], token):
        await websocket.close(code=4003, reason="Invalid host token")
        return

    room["host"] = websocket
    try:
        await websocket.send_json({"type": "auth_ok"})
        while True:
            got = await websocket.receive_json()
            if not isinstance(got, dict):
                continue
            if active_rooms.get(code) is not room:
                await websocket.send_json({"type": "room_closed"})
                break

            await handle_host_message(websocket, room, got)
    except Exception:
        pass
    finally:
        if room["host"] is websocket:
            room["host"] = None

async def handle_host_message(websocket, room, got):
    # deals with one frame from the host
    kind = got.get("type")

    if kind == "speaking":
        text = str(got.get("text", ""))[:spoken_max]
        await broadcast_to_listeners(room, {"type": "speaking", "text": text})

    elif kind == "audio_chunk":
        data = got.get("data", "")
        if not isinstance(data, str) or len(data) > char_max:
            await websocket.send_json({"type": "error", "message": "Clip too big. Send a smaller sample!"})
            return
        await broadcast_to_listeners(room, {"type": "audio_chunk", "data": data})

    elif kind == "listener_count":
        await websocket.send_json({"type": "listener_count", "count": len(room["listeners"])})

async def broadcast_to_listeners(room, payload):
    # helper for sending data to everyone in the room
    for ws in list(room["listeners"]):
        try:
            await ws.send_json(payload)
        except Exception:
            room["listeners"].discard(ws)

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)

    # run it
    uvicorn.run(
        app,
        host=os.environ.get("VOICEFLOW_HOST", "127.0.0.1"),
        port=int(os.environ.get("VOICEFLOW_PORT", "8000")),
    )
