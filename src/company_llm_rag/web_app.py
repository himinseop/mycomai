from typing import Dict, List

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from company_llm_rag.rag_system import rag_query
from company_llm_rag.teams_sender import send_inquiry_to_teams, is_inquiry_configured
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

app = FastAPI(title="슈퍼커넥트 AI 검색")

# 세션별 대화 히스토리 (session_id → messages)
_sessions: Dict[str, List[Dict]] = {}
_MAX_HISTORY_TURNS = 10

# RAG가 답변을 찾지 못했을 때 반환하는 문구
_NO_ANSWER_PHRASE = "관련 정보를 회사 지식베이스에서 찾을 수 없습니다."
_TEAMS_GUIDE = "\n\n아래 'Teams에 문의하기' 버튼을 통해 동료에게 직접 질문해보세요."


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    answer: str
    session_id: str
    inquiry_available: bool = False
    references: List[Dict] = []


class InquiryRequest(BaseModel):
    question: str
    session_id: str = "default"
    conversation_history: List[Dict] = []  # 프론트엔드에서 직접 전달 (서버 재시작 대비)


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("/app/company_llm_rag/templates/index.html", encoding="utf-8") as f:
        return f.read()


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    history = _sessions.setdefault(req.session_id, [])

    logger.info(f"[{req.session_id}] Query: {req.message}")
    answer, references = rag_query(req.message, conversation_history=history, return_refs=True)

    # 답변을 찾지 못한 경우 Teams 문의 가이드 안내 (자동 전송 없음)
    if _NO_ANSWER_PHRASE in answer and is_inquiry_configured():
        answer = answer + _TEAMS_GUIDE

    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": answer})

    max_messages = _MAX_HISTORY_TURNS * 2
    if len(history) > max_messages:
        _sessions[req.session_id] = history[-max_messages:]

    return ChatResponse(
        answer=answer,
        session_id=req.session_id,
        inquiry_available=is_inquiry_configured(),
        references=references,
    )


@app.post("/inquiry")
async def inquiry(req: InquiryRequest):
    """사용자가 수동으로 Teams 채널에 문의를 전송합니다."""
    if not is_inquiry_configured():
        return {"success": False, "message": "Teams 문의 채널이 설정되지 않았습니다."}

    # 프론트엔드 히스토리 우선 사용 (서버 재시작으로 세션이 초기화된 경우 대비)
    history = req.conversation_history or _sessions.get(req.session_id, [])
    success = send_inquiry_to_teams(req.question, history)
    return {
        "success": success,
        "message": "Teams 채널에 문의가 전송됐습니다." if success else "전송에 실패했습니다. 잠시 후 다시 시도해주세요.",
    }


@app.delete("/chat/{session_id}")
async def clear_session(session_id: str):
    _sessions.pop(session_id, None)
    return {"status": "cleared", "session_id": session_id}
