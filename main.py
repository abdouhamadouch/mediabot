
import os
import re
import asyncio
import logging
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlparse, unquote
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field
from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality, VideoQuality


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("CallEngine")

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")
ENGINE_SECRET = os.getenv("CALL_ENGINE_SECRET", "")

TEMP_DIR = Path(tempfile.gettempdir()) / "mediabot_media"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


def parse_telegram_post(url: str):
    """Return (chat reference, message ID) for a Telegram post URL."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        return None

    parts = [unquote(p) for p in parsed.path.split("/") if p]

    # Support public preview links: t.me/s/channel/123
    if parts and parts[0] == "s":
        parts = parts[1:]

    if len(parts) < 2:
        return None

    # Private-channel post links: t.me/c/123456789/123
    if parts[0] == "c":
        if len(parts) < 3:
            return None

        channel_id, message_id = parts[1], parts[2]

        if not channel_id.isdigit() or not message_id.isdigit():
            return None

        return int(f"-100{channel_id}"), int(message_id)

    # Public-channel or group post links: t.me/channelname/123
    username, message_id = parts[0], parts[1]

    if not message_id.isdigit():
        return None

    if username.lower() in {
        "joinchat", "share", "addstickers", "proxy",
        "iv", "login", "confirmphone"
    }:
        return None

    return username, int(message_id)


def is_youtube_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }


def remove_temp_file(path):
    if not path:
        return

    try:
        candidate = Path(path).resolve()
        temp_root = TEMP_DIR.resolve()

        if candidate.is_relative_to(temp_root) and candidate.is_file():
            candidate.unlink(missing_ok=True)

    except (OSError, ValueError):
        log.warning("Could not remove temporary media file.")


class CallEngine:
    def __init__(self):
        self.app = None
        self.call = None
        self._start_lock = asyncio.Lock()
        self._media_files = {}

    async def start(self):
        async with self._start_lock:
            if self.call is not None:
                return

            if not API_ID or not API_HASH or not SESSION_STRING:
                raise RuntimeError(
                    "API_ID, API_HASH, and SESSION_STRING are required."
                )

            if not ENGINE_SECRET:
                raise RuntimeError(
                    "CALL_ENGINE_SECRET environment variable is required."
                )

            client = Client(
                "assistant_userbot",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=SESSION_STRING,
            )

            try:
                await client.start()

                call = PyTgCalls(client)
                await call.start()

                self.app = client
                self.call = call

                log.info("Call Engine started successfully.")

            except Exception:
                log.exception("Failed to start Call Engine.")

                try:
                    await client.stop()
                except Exception:
                    log.exception("Failed to stop client after startup failure.")

                raise

    async def resolve_media(self, url: str, chat_id: int):
        """
        Direct URLs and YouTube URLs are passed to PyTgCalls.
        Telegram post URLs are resolved and their media is downloaded.
        """
        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("The media URL must be a valid HTTP or HTTPS URL.")

        telegram_post = parse_telegram_post(url)

        if not telegram_post:
            return url, None

        if not self.app:
            raise RuntimeError("Telegram client is not initialized.")

        telegram_chat, message_id = telegram_post

        message = await self.app.get_messages(
            telegram_chat,
            message_id,
        )

        if not message or not message.media:
            raise ValueError(
                "The Telegram post was not found or does not contain media."
            )

        downloaded_path = await self.app.download_media(
            message,
            file_name=str(
                TEMP_DIR / f"telegram_{chat_id}_{message_id}"
            ),
        )

        if not downloaded_path:
            raise ValueError(
                "Could not download media from the Telegram post."
            )

        return downloaded_path, downloaded_path

    async def play(self, chat_id: int, url: str, video: bool = False):
        if not self.call:
            raise RuntimeError("Engine is not initialized.")

        source, downloaded_path = await self.resolve_media(url, chat_id)

        try:
            if downloaded_path:
                # A Telegram post was downloaded to a local file.
                if video:
                    stream = MediaStream(
                        source,
                        AudioQuality.HIGH,
                        VideoQuality.HD_720p,
                    )
                else:
                    stream = MediaStream(
                        source,
                        AudioQuality.HIGH,
                        video_flags=MediaStream.Flags.IGNORE,
                    )

            elif is_youtube_url(source):
                # PyTgCalls uses yt-dlp for YouTube URLs.
                if video:
                    stream = MediaStream(
                        source,
                        AudioQuality.HIGH,
                        VideoQuality.HD_720p,
                        ytdlp_parameters="--no-warnings",
                    )
                else:
                    stream = MediaStream(
                        source,
                        AudioQuality.HIGH,
                        video_flags=MediaStream.Flags.IGNORE,
                        ytdlp_parameters="--no-warnings",
                    )

            else:
                # Direct audio/video URL.
                if video:
                    stream = MediaStream(
                        source,
                        AudioQuality.HIGH,
                        VideoQuality.HD_720p,
                    )
                else:
                    stream = MediaStream(
                        source,
                        AudioQuality.HIGH,
                        video_flags=MediaStream.Flags.IGNORE,
                    )

            await self.call.play(chat_id, stream)

        except Exception:
            remove_temp_file(downloaded_path)
            log.exception("Failed to start media playback.")
            raise

        old_path = self._media_files.get(chat_id)

        if downloaded_path:
            self._media_files[chat_id] = downloaded_path
        else:
            self._media_files.pop(chat_id, None)

        if old_path and old_path != downloaded_path:
            remove_temp_file(old_path)

        log.info(
            "Started %s playback in chat %s",
            "video" if video else "audio",
            chat_id,
        )

        return {
            "chat_id": chat_id,
            "video": video,
            "source_type": (
                "telegram"
                if downloaded_path
                else "youtube"
                if is_youtube_url(source)
                else "direct"
            ),
        }

    async def pause(self, chat_id: int):
        if not self.call:
            raise RuntimeError("Engine is not initialized.")

        await self.call.pause(chat_id)

    async def resume(self, chat_id: int):
        if not self.call:
            raise RuntimeError("Engine is not initialized.")

        await self.call.resume(chat_id)

    async def leave(self, chat_id: int):
        if not self.call:
            raise RuntimeError("Engine is not initialized.")

        await self.call.leave_call(chat_id)

        path = self._media_files.pop(chat_id, None)
        remove_temp_file(path)

    async def stop(self):
        call = self.call
        client = self.app

        self.call = None
        self.app = None

        if call:
            try:
                await call.stop()
            except Exception:
                log.exception("Error stopping PyTgCalls.")

        if client:
            try:
                await client.stop()
            except Exception:
                log.exception("Error stopping Telegram client.")

        for path in self._media_files.values():
            remove_temp_file(path)

        self._media_files.clear()


call_engine = CallEngine()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await call_engine.start()

    try:
        yield
    finally:
        await call_engine.stop()


app = FastAPI(
    title="Voice Call Stream Engine",
    lifespan=lifespan,
)


class StreamRequest(BaseModel):
    chat_id: int
    url: str = Field(min_length=1, max_length=4096)
    video: bool = False


class ActionRequest(BaseModel):
    chat_id: int


def verify_auth(authorization: str | None = Header(default=None)):
    if not ENGINE_SECRET:
        raise HTTPException(
            status_code=503,
            detail="API authentication is not configured.",
        )

    expected = f"Bearer {ENGINE_SECRET}"

    if not authorization or not secrets.compare_digest(
        authorization,
        expected,
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )


@app.get("/")
async def home():
    return {
        "status": "running",
        "engine": "Voice & Video Call Streamer",
    }


@app.post("/play")
async def play_media(
    req: StreamRequest,
    authorization: str | None = Header(default=None),
):
    verify_auth(authorization)

    try:
        result = await call_engine.play(
            req.chat_id,
            req.url,
            video=req.video,
        )

        return {
            "ok": True,
            "result": result,
        }

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    except Exception:
        log.exception("Playback request failed.")

        raise HTTPException(
            status_code=500,
            detail="Playback failed. Check Render logs.",
        )


@app.post("/pause")
async def pause_media(
    req: ActionRequest,
    authorization: str | None = Header(default=None),
):
    verify_auth(authorization)

    try:
        await call_engine.pause(req.chat_id)
        return {"ok": True}

    except Exception:
        log.exception("Pause request failed.")
        raise HTTPException(status_code=500, detail="Pause failed.")


@app.post("/resume")
async def resume_media(
    req: ActionRequest,
    authorization: str | None = Header(default=None),
):
    verify_auth(authorization)

    try:
        await call_engine.resume(req.chat_id)
        return {"ok": True}

    except Exception:
        log.exception("Resume request failed.")
        raise HTTPException(status_code=500, detail="Resume failed.")


@app.post("/stop")
async def stop_media(
    req: ActionRequest,
    authorization: str | None = Header(default=None),
):
    verify_auth(authorization)

    try:
        await call_engine.leave(req.chat_id)
        return {"ok": True}

    except Exception:
        log.exception("Stop request failed.")
        raise HTTPException(status_code=500, detail="Stop failed.")
