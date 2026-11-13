import os, io, re, json, time, secrets, asyncio, logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse


import httpx
from fastapi import FastAPI, Request, HTTPException, Header, Form, File, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator


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

allowed_ogs = {

          o.strip().rstrip("/").lower()

              for o in os.environ.get("VOICEFLOW_ALLOWED_ORIGINS", "").split(",")

                  if o.strip()

}



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

                                                         responses.headers.setdefault("Content-Security-Pol")
                                         )
}