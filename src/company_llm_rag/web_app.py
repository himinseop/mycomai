from typing import Dict, List

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from company_llm_rag.rag_system import rag_query
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

app = FastAPI(title="Mycomai RAG Chat")

# 세션별 대화 히스토리 (session_id → messages)
_sessions: Dict[str, List[Dict]] = {}
_MAX_HISTORY_TURNS = 10


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    answer: str
    session_id: str


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("/app/company_llm_rag/templates/index.html", encoding="utf-8") as f:
        return f.read()


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    history = _sessions.setdefault(req.session_id, [])

    logger.info(f"[{req.session_id}] Query: {req.message}")
    answer = rag_query(req.message, conversation_history=history)

    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": answer})

    # 최대 턴 수 유지
    max_messages = _MAX_HISTORY_TURNS * 2
    if len(history) > max_messages:
        _sessions[req.session_id] = history[-max_messages:]

    return ChatResponse(answer=answer, session_id=req.session_id)


@app.delete("/chat/{session_id}")
async def clear_session(session_id: str):
    _sessions.pop(session_id, None)
    return {"status": "cleared", "session_id": session_id}
