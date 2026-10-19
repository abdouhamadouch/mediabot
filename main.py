import os
import asyncio
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from engine import call_engine

app = FastAPI(title="Voice Call Stream Engine")

ENGINE_SECRET = os.getenv("CALL_ENGINE_SECRET", "my_secret_token")

class StreamRequest(BaseModel):
    chat_id: int
    url: str
    video: bool = False

class ActionRequest(BaseModel):
    chat_id: int

def verify_auth(authorization: str = Header(None)):
    if not authorization or authorization != f"Bearer {ENGINE_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.on_event("startup")
async def on_startup():
    await call_engine.start()

@app.on_event("shutdown")
async def on_shutdown():
    await call_engine.stop()

@app.get("/")
def home():
    return {"status": "running", "engine": "Voice & Video Call Streamer"}

@app.post("/play")
async def play_media(req: StreamRequest, authorization: str = Header(None)):
    verify_auth(authorization)
    try:
        res = await call_engine.play(req.chat_id, req.url, video=req.video)
        return {"ok": True, "result": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/pause")
async def pause_media(req: ActionRequest, authorization: str = Header(None)):
    verify_auth(authorization)
    await call_engine.pause(req.chat_id)
    return {"ok": True}

@app.post("/resume")
async def resume_media(req: ActionRequest, authorization: str = Header(None)):
    verify_auth(authorization)
    await call_engine.resume(req.chat_id)
    return {"ok": True}

@app.post("/stop")
async def stop_media(req: ActionRequest, authorization: str = Header(None)):
    verify_auth(authorization)
    await call_engine.leave(req.chat_id)
    return {"ok": True}
