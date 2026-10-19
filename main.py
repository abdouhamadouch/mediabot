
import os
import asyncio
import logging
import secrets

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality, VideoQuality

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("CallEngine")

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

# Multiple API keys, separated by commas.
# Keeps compatibility with the existing CALL_ENGINE_SECRET setting.
_raw_secrets = os.getenv("CALL_ENGINE_SECRETS", "").strip()

if _raw_secrets:
    ENGINE_SECRETS = {
        secret.strip()
        for secret in _raw_secrets.split(",")
        if secret.strip()
    }
else:
    ENGINE_SECRET = os.getenv("CALL_ENGINE_SECRET", "my_secret_token").strip()
    ENGINE_SECRETS = {ENGINE_SECRET} if ENGINE_SECRET else set()


class CallEngine:
    def __init__(self):
        self.app = None
        self.call = None

    async def start(self):
        if not API_ID or not API_HASH or not SESSION_STRING:
            raise RuntimeError(
                "API_ID, API_HASH, and SESSION_STRING are required."
            )

        self.app = Client(
            "assistant_userbot",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=SESSION_STRING
        )
        await self.app.start()
        self.call = PyTgCalls(self.app)
        await self.call.start()
        log.info("Call Engine started successfully.")

    async def play(self, chat_id: int, url: str, video: bool = False):
        if not self.call:
            raise RuntimeError("Engine not initialized")

        if video:
            stream = MediaStream(
                url,
                audio_parameters=AudioQuality.HIGH,
                video_parameters=VideoQuality.HD_720p
            )
        else:
            stream = MediaStream(
                url,
                audio_parameters=AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE
            )

        await self.call.play(chat_id, stream)
        log.info(
            f"Streaming {'video' if video else 'audio'} in chat {chat_id}"
        )
        return {"chat_id": chat_id, "video": video}

    async def pause(self, chat_id: int):
        if self.call:
            await self.call.pause(chat_id)

    async def resume(self, chat_id: int):
        if self.call:
            await self.call.resume(chat_id)

    async def leave(self, chat_id: int):
        if self.call:
            try:
                await self.call.leave_call(chat_id)
            except Exception as e:
                log.warning(f"Error leaving call: {e}")

    async def stop(self):
        if self.call:
            await self.call.stop()
        if self.app:
            await self.app.stop()


call_engine = CallEngine()
app = FastAPI(title="Voice Call Stream Engine")


class StreamRequest(BaseModel):
    chat_id: int
    url: str
    video: bool = False


class ActionRequest(BaseModel):
    chat_id: int


def verify_auth(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    supplied_secret = authorization[len("Bearer "):].strip()

    if not any(
        secrets.compare_digest(supplied_secret, allowed_secret)
        for allowed_secret in ENGINE_SECRETS
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.on_event("startup")
async def on_startup():
    await call_engine.start()


@app.on_event("shutdown")
async def on_shutdown():
    await call_engine.stop()


@app.get("/")
def home():
    return {
        "status": "running",
        "engine": "Voice & Video Call Streamer"
    }


@app.post("/play")
async def play_media(
    req: StreamRequest,
    authorization: str = Header(None)
):
    verify_auth(authorization)
    try:
        res = await call_engine.play(
            req.chat_id,
            req.url,
            video=req.video
        )
        return {"ok": True, "result": res}
    except Exception as e:
        log.exception("Failed to play media")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/pause")
async def pause_media(
    req: ActionRequest,
    authorization: str = Header(None)
):
    verify_auth(authorization)
    await call_engine.pause(req.chat_id)
    return {"ok": True}


@app.post("/resume")
async def resume_media(
    req: ActionRequest,
    authorization: str = Header(None)
):
    verify_auth(authorization)
    await call_engine.resume(req.chat_id)
    return {"ok": True}


@app.post("/stop")
async def stop_media(
    req: ActionRequest,
    authorization: str = Header(None)
):
    verify_auth(authorization)
    await call_engine.leave(req.chat_id)
    return {"ok": True}
