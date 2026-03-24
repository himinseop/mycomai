import asyncio
import base64
import json
import time
from typing import Dict, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from company_llm_rag.config import settings
from company_llm_rag.rag_system import rag_query, rag_query_stream, _NO_ANSWER_PHRASE
from company_llm_rag.teams_sender import (
    send_inquiry_to_teams,
    send_feedback_alert_to_teams,
    is_inquiry_configured,
)
from company_llm_rag.history_store import (
    init_db, save as history_save, save_feedback,
    get_session_history, get_stats, SESSION_TTL_DAYS,
    get_setting, set_setting,
    get_history_page, get_record_detail,
)
from company_llm_rag.no_answer_analyzer import analyze_no_answer, analyze_with_answer
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
    docs_holder: list = []
    answer, references, timing = rag_query(req.message, conversation_history=history, return_refs=True, _docs_out=docs_holder)
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
        perf=timing,
    )

    # 결과보고서 작성: 설정 ON일 때만 백그라운드 실행
    if get_setting("analyze_no_answer", "0") == "1":
        if is_no_answer:
            asyncio.create_task(analyze_no_answer(record_id, req.message))
        else:
            asyncio.create_task(analyze_with_answer(record_id, req.message, answer, references, list(docs_holder)))

    return ChatResponse(
        answer=answer,
        session_id=req.session_id,
        inquiry_available=is_inquiry_configured(),
        references=references,
        record_id=record_id,
    )


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """SSE 스트리밍 채팅 엔드포인트. 토큰이 생성될 때마다 즉시 전송합니다."""
    history = _sessions.setdefault(req.session_id, [])
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    docs_holder: list = []  # 검색된 전체 문서 캡처 (결과분석용)

    def _run():
        try:
            for ev in rag_query_stream(req.message, conversation_history=history, _docs_out=docs_holder):
                loop.call_soon_threadsafe(queue.put_nowait, ev)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "message": str(e)})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    async def generate():
        loop.run_in_executor(None, _run)
        done_event = None
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev.get("type") == "done":
                done_event = ev

        if done_event:
            answer = done_event.get("answer", "")
            references = done_event.get("references", [])
            timing = done_event.get("timing", {})
            is_no_answer = done_event.get("is_no_answer", False)

            if is_no_answer and is_inquiry_configured():
                answer = answer + _TEAMS_GUIDE

            history.append({"role": "user", "content": req.message})
            history.append({"role": "assistant", "content": answer})
            max_messages = _MAX_HISTORY_TURNS * 2
            if len(history) > max_messages:
                _sessions[req.session_id] = history[-max_messages:]

            record_id = history_save(
                req.session_id, req.message, answer, references,
                response_time_ms=timing.get("total_ms"),
                is_no_answer=is_no_answer,
                perf=timing,
            )

            meta_ev = {
                "type": "meta",
                "record_id": record_id,
                "inquiry_available": is_inquiry_configured(),
                "is_no_answer": is_no_answer,
            }
            yield f"data: {json.dumps(meta_ev, ensure_ascii=False)}\n\n"

            # 결과보고서 작성: 설정 ON일 때만 백그라운드 실행
            if get_setting("analyze_no_answer", "0") == "1":
                if is_no_answer:
                    asyncio.create_task(analyze_no_answer(record_id, req.message))
                else:
                    asyncio.create_task(analyze_with_answer(record_id, req.message, answer, references, list(docs_holder)))

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/feedback")
async def feedback(req: FeedbackRequest):
    if req.rating not in (1, -1):
        return {"success": False, "message": "rating은 1 또는 -1이어야 합니다."}

    ok = save_feedback(req.record_id, req.rating)

    # 👎인 경우 Teams 알림 — DB에서 question/answer 직접 조회 (클라이언트 전달값 미사용)
    if ok and req.rating == -1 and is_inquiry_configured():
        record = get_record_detail(req.record_id)
        if record:
            send_feedback_alert_to_teams(record["question"], record["answer"])

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
        response = HTMLResponse(content="Unauthorized", status_code=401)
        response.raw_headers.append(
            (b"www-authenticate", 'Basic realm="오사장 어드민"'.encode("utf-8"))
        )
        return response
    with open("/app/company_llm_rag/templates/admin.html", encoding="utf-8") as f:
        return f.read()


@app.get("/admin/stats")
async def admin_stats(request: Request, days: int = 14):
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return get_stats(days=days)


@app.get("/admin/history/data")
async def admin_history_data(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    is_no_answer: Optional[int] = Query(None),
    feedback: Optional[int] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
):
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return get_history_page(
        page=page,
        page_size=page_size,
        is_no_answer=is_no_answer,
        feedback=feedback,
        date_from=date_from,
        date_to=date_to,
        q=q,
    )


@app.get("/admin/history/{record_id}/detail")
async def admin_history_detail(request: Request, record_id: int):
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    detail = get_record_detail(record_id)
    if not detail:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return detail


@app.get("/admin/history/{record_id}/analysis")
async def admin_history_analysis(request: Request, record_id: int):
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    detail = get_record_detail(record_id)
    if not detail:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {
        "record_id": record_id,
        "analysis_status": detail["analysis_status"],
        "no_answer_analysis": detail["no_answer_analysis"],
    }


@app.get("/admin/settings/data")
async def admin_settings_get(request: Request):
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return {
        "analyze_no_answer": get_setting("analyze_no_answer", "0") == "1",
    }


class SettingsUpdateRequest(BaseModel):
    analyze_no_answer: Optional[bool] = None


@app.post("/admin/settings")
async def admin_settings_update(request: Request, body: SettingsUpdateRequest):
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if body.analyze_no_answer is not None:
        set_setting("analyze_no_answer", "1" if body.analyze_no_answer else "0")
    return {"success": True}
