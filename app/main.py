"""HTTP server: the chat page and a streaming chat endpoint (server-sent events)."""
import asyncio
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import agent, kb

app = FastAPI(title="DarGlobal & Wasalt property assistant")
STATIC = os.path.join(os.path.dirname(__file__), "static")


class Turn(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default_factory=list, max_length=20)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/health")
async def health():
    return {"ok": True, "kb": kb.meta(), "models": agent.MODELS, "model_calls_today": agent._budget["used"]}


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    async def stream():
        """Agent events as SSE, with a comment line every 15 s so proxies keep a slow model call open."""
        queue = asyncio.Queue()

        async def produce():
            try:
                async for ev in agent.run(body.message, [t.model_dump() for t in body.history]):
                    await queue.put(ev)
            finally:
                await queue.put(None)

        task = asyncio.create_task(produce())
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if ev is None or await request.is_disconnected():
                    break
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
        finally:
            task.cancel()
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
