import base64
import time
from typing import Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from company_llm_rag.config import settings
from company_llm_rag.rag_system import rag_query, _NO_ANSWER_PHRASE
from company_llm_rag.teams_sender import (
    send_inquiry_to_teams,
    send_feedback_alert_to_teams,
    is_inquiry_configured,
)
from company_llm_rag.history_store import (
    init_db, save as history_save, save_feedback,
    get_session_history, get_stats, SESSION_TTL_DAYS,
)
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

app = FastAPI(title="오사장 - 슈퍼커넥트 AI")
app.mount("/static", StaticFiles(directory="/app/company_llm_rag/static"), name="static")

# DB 초기화 (앱 시작 시 마이그레이션 + 만료 레코드 정리)
init_db()

# 세션별 대화 히스토리 (session_id → messages) — 서버 메모리 캐시
_sessions: Dict[str, List[Dict]] = {}
_MAX_HISTORY_TURNS = 10

_TEAMS_GUIDE = "\n\n아래 'Teams에 문의하기' 버튼을 통해 동료에게 직접 질문해보세요."


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    answer: str
    session_id: str
    inquiry_available: bool = False
    references: List[Dict] = []
    record_id: int = 0


class InquiryRequest(BaseModel):
    question: str
    session_id: str = "default"
    conversation_history: List[Dict] = []


class FeedbackRequest(BaseModel):
    record_id: int
    rating: int          # 1(👍) 또는 -1(👎)
    question: str = ""
    answer: str = ""
    session_id: str = "default"


def _check_admin_auth(request: Request) -> bool:
    """HTTP Basic Auth로 어드민 접근을 검증합니다."""
    if not settings.ADMIN_PASSWORD:
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        _, password = decoded.split(":", 1)
        return password == settings.ADMIN_PASSWORD
    except Exception:
        return False


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("/app/company_llm_rag/templates/index.html", encoding="utf-8") as f:
        return f.read()


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    history = _sessions.setdefault(req.session_id, [])
    logger.info(f"[{req.session_id}] Query: {req.message}")

    t_start = time.monotonic()
    answer, references = rag_query(req.message, conversation_history=history, return_refs=True)
    response_time_ms = int((time.monotonic() - t_start) * 1000)

    is_no_answer = _NO_ANSWER_PHRASE in answer

    if is_no_answer and is_inquiry_configured():
        answer = answer + _TEAMS_GUIDE

    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": answer})

    max_messages = _MAX_HISTORY_TURNS * 2
    if len(history) > max_messages:
        _sessions[req.session_id] = history[-max_messages:]

    record_id = history_save(
        req.session_id, req.message, answer, references,
        response_time_ms=response_time_ms,
        is_no_answer=is_no_answer,
    )

    return ChatResponse(
        answer=answer,
        session_id=req.session_id,
        inquiry_available=is_inquiry_configured(),
        references=references,
        record_id=record_id,
    )


@app.post("/feedback")
async def feedback(req: FeedbackRequest):
    if req.rating not in (1, -1):
        return {"success": False, "message": "rating은 1 또는 -1이어야 합니다."}

    ok = save_feedback(req.record_id, req.rating)

    # 👎인 경우 Teams 알림
    if ok and req.rating == -1 and is_inquiry_configured():
        send_feedback_alert_to_teams(req.question, req.answer)

    return {"success": ok}


@app.post("/inquiry")
async def inquiry(req: InquiryRequest):
    if not is_inquiry_configured():
        return {"success": False, "message": "Teams 문의 채널이 설정되지 않았습니다."}

    history = req.conversation_history or _sessions.get(req.session_id, [])
    success = send_inquiry_to_teams(req.question, history)
    return {
        "success": success,
        "message": "Teams 채널에 문의가 전송됐습니다." if success else "전송에 실패했습니다. 잠시 후 다시 시도해주세요.",
    }


@app.get("/history/{session_id}")
async def get_history(session_id: str):
    """세션의 질문 이력을 반환합니다."""
    records = get_session_history(session_id)
    return {
        "session_id": session_id,
        "session_ttl_days": SESSION_TTL_DAYS,
        "count": len(records),
        "records": records,
    }


@app.delete("/chat/{session_id}")
async def clear_session(session_id: str):
    _sessions.pop(session_id, None)
    return {"status": "cleared", "session_id": session_id}


# ── 어드민 대시보드 ──────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not _check_admin_auth(request):
        return HTMLResponse(
            content="Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Admin"'},
        )
    with open("/app/company_llm_rag/templates/admin.html", encoding="utf-8") as f:
        return f.read()


@app.get("/admin/stats")
async def admin_stats(request: Request, days: int = 14):
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return get_stats(days=days)
