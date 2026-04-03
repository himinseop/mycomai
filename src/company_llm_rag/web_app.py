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
    init_db, save as history_save, invalidate_stats_cache,
    save_record_feedback, save_group_feedback,
    get_session_history, get_stats, SESSION_TTL_DAYS,
    get_setting, set_setting,
    get_history_page, get_record_detail,
    get_collection_dates,
    get_last_turn_in_session,
    get_session_groups, get_session_detail,
    set_group_analysis_pending,
)
from company_llm_rag.no_answer_analyzer import analyze_bad_feedback
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)


def _compact_retrieved_docs(docs: list) -> list:
    """retrieved_docs를 DB 저장용 compact 형식으로 변환합니다 (content 제외)."""
    result = []
    for d in docs:
        meta = d.get("metadata", {})
        url = meta.get("url", "") or ""
        if not url:
            from company_llm_rag.rag_system import _build_teams_url
            url = _build_teams_url(meta)
        result.append({
            "source": meta.get("source", ""),
            "title": meta.get("title", "") or "",
            "url": url,
            "_rrf": d.get("_rrf", 0),
            "_vector_rank": d.get("_vector_rank"),
            "_keyword_rank": d.get("_keyword_rank"),
            "_injected": d.get("_injected", False),
            "_distance": d.get("_distance", 1.0),
        })
    return result


app = FastAPI(title="오사장 - 슈퍼커넥트 AI")
app.mount("/static", StaticFiles(directory="/app/company_llm_rag/static"), name="static")

# DB 초기화 (앱 시작 시 마이그레이션 + 만료 레코드 정리)
init_db()


@app.on_event("startup")
async def _warmup_reranker():
    """Reranker 모델을 서버 시작 시 미리 로딩합니다."""
    if settings.RERANKER_ENABLED:
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _load_reranker)


def _load_reranker():
    from company_llm_rag.reranker.factory import get_reranker
    reranker = get_reranker()
    if reranker and hasattr(reranker, '_load'):
        reranker._load()


@app.on_event("startup")
async def _warmup_db_stats():
    """앱 시작 시 db-stats 캐시를 백그라운드로 갱신합니다."""
    import asyncio
    loop = asyncio.get_event_loop()

    async def _run():
        global _db_stats_cache, _db_stats_cache_time
        try:
            result = await loop.run_in_executor(None, _compute_db_stats)
            _db_stats_cache = result
            _db_stats_cache_time = time.monotonic()
            logger.info("[Admin] db-stats 캐시 워밍업 완료")
        except Exception as e:
            logger.warning(f"[Admin] db-stats 워밍업 실패: {e}")

    asyncio.ensure_future(_run())

# 세션별 대화 히스토리 (session_id → messages) — 서버 메모리 캐시
_sessions: Dict[str, List[Dict]] = {}
_MAX_HISTORY_TURNS = 10

_TEAMS_GUIDE = "\n\n아래 '담당자에게 문의하기' 버튼을 통해 질문을 남겨주세요."


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    answer: str
    session_id: str
    inquiry_available: bool = False
    references: List[Dict] = []
    record_id: int = 0
    turn_index: int = 1
    is_group_root: bool = True


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
    conversation_history: List[Dict] = []
    scope: str = "group"  # "group" | "record"


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
    company_name = settings.COMPANY_NAME or "오사장"
    with open("/app/company_llm_rag/templates/index.html", encoding="utf-8") as f:
        html = f.read()
    return html.replace("{{ company_name }}", company_name)


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    history = _sessions.setdefault(req.session_id, [])
    logger.info(f"[{req.session_id}] Query: {req.message}")

    # 질문 그룹 내 turn 계산
    last_turn = get_last_turn_in_session(req.session_id)
    if last_turn:
        turn_index = last_turn["turn_index"] + 1
        parent_record_id = last_turn["id"]
    else:
        turn_index = 1
        parent_record_id = None

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
        turn_index=turn_index,
        parent_record_id=parent_record_id,
        retrieved_docs=_compact_retrieved_docs(docs_holder),
    )
    invalidate_stats_cache()

    return ChatResponse(
        answer=answer,
        session_id=req.session_id,
        inquiry_available=is_inquiry_configured(),
        references=references,
        record_id=record_id,
        turn_index=turn_index,
        is_group_root=(turn_index == 1),
    )


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """SSE 스트리밍 채팅 엔드포인트. 토큰이 생성될 때마다 즉시 전송합니다."""
    history = _sessions.setdefault(req.session_id, [])
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    docs_holder: list = []  # 검색된 전체 문서 캡처 (결과분석용)

    # 질문 그룹 내 turn 계산 (스트리밍 시작 전에 확정)
    last_turn = get_last_turn_in_session(req.session_id)
    if last_turn:
        turn_index = last_turn["turn_index"] + 1
        parent_record_id: Optional[int] = last_turn["id"]
    else:
        turn_index = 1
        parent_record_id = None

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
                turn_index=turn_index,
                parent_record_id=parent_record_id,
                retrieved_docs=_compact_retrieved_docs(docs_holder),
            )
            invalidate_stats_cache()

            meta_ev = {
                "type": "meta",
                "record_id": record_id,
                "session_id": req.session_id,
                "turn_index": turn_index,
                "is_group_root": turn_index == 1,
                "inquiry_available": is_inquiry_configured(),
                "is_no_answer": is_no_answer,
            }
            yield f"data: {json.dumps(meta_ev, ensure_ascii=False)}\n\n"


    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/feedback")
async def feedback(req: FeedbackRequest):
    if req.rating not in (1, -1):
        return {"success": False, "message": "rating은 1 또는 -1이어야 합니다."}

    # scope 분기: group(기본) → 세션 전체, record → 단건
    if req.scope == "group" and req.session_id and req.session_id != "default":
        ok = save_group_feedback(req.session_id, req.rating)
    else:
        ok = save_record_feedback(req.record_id, req.rating)

    if ok and req.rating == -1:
        # DB에서 question/answer 직접 조회 (클라이언트 전달값 미사용)
        record = get_record_detail(req.record_id)
        if record:
            # Teams 알림
            if is_inquiry_configured():
                send_feedback_alert_to_teams(record["question"], record["answer"])
            # 결과보고서: 설정 ON이고 👎일 때만 백그라운드 분석
            if get_setting("analyze_no_answer", "0") == "1":
                asyncio.create_task(
                    analyze_bad_feedback(
                        req.record_id,
                        record["question"],
                        record["answer"],
                        bool(record["is_no_answer"]),
                        session_id=req.session_id if req.scope == "group" else None,
                        group_feedback=-1,
                    )
                )

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

_db_stats_cache: dict = {}
_db_stats_cache_time: float = 0.0


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not _check_admin_auth(request):
        response = HTMLResponse(content="Unauthorized", status_code=401)
        response.raw_headers.append(
            (b"www-authenticate", 'Basic realm="오사장 어드민"'.encode("utf-8"))
        )
        return response
    company_name = settings.COMPANY_NAME or "오사장"
    with open("/app/company_llm_rag/templates/admin.html", encoding="utf-8") as f:
        html = f.read()
    return html.replace("{{ company_name }}", company_name)


def _compute_db_stats() -> dict:
    """ChromaDB 소스별 통계를 계산합니다. 동기 함수 (executor에서 호출)."""
    from company_llm_rag.database import db_manager
    import datetime as _dt

    collection = db_manager.get_collection()

    # 소스별 표시 레이블과 상세 분류 기준 메타데이터 키
    _SOURCES = {
        "jira":       {"label": "일감",  "group_key": "jira_project_key"},
        "confluence": {"label": "페이지", "group_key": "confluence_space_name"},
        "sharepoint": {"label": "파일",  "group_key": "sharepoint_site_name"},
        "teams":      {"label": "메시지", "group_key": "teams_channel_name", "parent_key": "teams_team_name"},
    }

    # Step 1: ID만 조회 → 소스별 청크 수 집계
    id_res = collection.get(include=[])
    chunk_counts: dict = {}
    for chunk_id in id_res["ids"]:
        src = chunk_id.split("-")[0]
        chunk_counts[src] = chunk_counts.get(src, 0) + 1

    # Step 2: 소스별 메타데이터 조회 → 문서 수 + 상세 분류
    collection_dates = get_collection_dates()
    source_stats: dict = {}

    for src, cfg in _SOURCES.items():
        if not chunk_counts.get(src):
            continue

        meta_res = collection.get(where={"source": src}, include=["metadatas"])

        doc_group: dict = {}  # original_doc_id → 그룹 레이블
        for meta in meta_res["metadatas"]:
            doc_id = meta.get("original_doc_id", "")
            if not doc_id:
                continue
            if src == "teams":
                parent = meta.get("teams_team_name") or ""
                child  = meta.get("teams_channel_name") or ""
                if parent and child and parent != child:
                    group = f"{parent} / {child}"
                else:
                    group = parent or child or "기타"
            else:
                group = meta.get(cfg["group_key"]) or "기타"
            doc_group[doc_id] = group

        # 그룹별 문서 수 집계 (내림차순 정렬)
        detail: dict = {}
        for grp in doc_group.values():
            detail[grp] = detail.get(grp, 0) + 1
        detail = dict(sorted(detail.items(), key=lambda x: x[1], reverse=True))

        source_stats[src] = {
            "label": cfg["label"],
            "docs": len(doc_group),
            "chunks": chunk_counts[src],
            "latest": collection_dates.get(src),
            "detail": detail,
        }

    return {
        "total_docs": sum(s["docs"] for s in source_stats.values()),
        "total_chunks": sum(chunk_counts.values()),
        "sources": source_stats,
        "cached_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }


@app.get("/admin/db-stats")
async def admin_db_stats(request: Request):
    """ChromaDB 소스별 문서 수 + 최근 수집일자를 반환합니다. 24시간 캐싱."""
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    global _db_stats_cache, _db_stats_cache_time
    if _db_stats_cache_time > 0 and time.monotonic() - _db_stats_cache_time < 86400:
        return _db_stats_cache
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _compute_db_stats)
    _db_stats_cache = result
    _db_stats_cache_time = time.monotonic()
    return _db_stats_cache


@app.post("/admin/db-stats/refresh")
async def admin_db_stats_refresh(request: Request):
    """db-stats 캐시를 즉시 강제 갱신합니다."""
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    global _db_stats_cache, _db_stats_cache_time
    _db_stats_cache_time = 0.0  # 캐시 무효화
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _compute_db_stats)
    _db_stats_cache = result
    _db_stats_cache_time = time.monotonic()
    return {**result, "refreshed": True}


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


@app.post("/admin/history/{record_id}/analyze")
async def admin_trigger_analysis(request: Request, record_id: int):
    """설정 스위치 무관하게 수동으로 결과분석을 실행합니다."""
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    detail = get_record_detail(record_id)
    if not detail:
        return JSONResponse({"error": "Not found"}, status_code=404)
    if detail["analysis_status"] == "pending":
        return {"success": False, "message": "이미 분석 중입니다."}
    asyncio.create_task(
        analyze_bad_feedback(
            record_id,
            detail["question"],
            detail["answer"],
            bool(detail["is_no_answer"]),
        )
    )
    return {"success": True}


class AdminFeedbackRequest(BaseModel):
    rating: int


@app.post("/admin/sessions/{session_id}/feedback")
async def admin_session_feedback(request: Request, session_id: str, body: AdminFeedbackRequest):
    """관리자 전용 그룹 피드백 저장 (부작용 없음)."""
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if body.rating not in (1, -1):
        return JSONResponse({"error": "rating은 1 또는 -1"}, status_code=400)
    ok = save_group_feedback(session_id, body.rating)
    return {"success": ok}


@app.post("/admin/sessions/{session_id}/analyze")
async def admin_session_analyze(request: Request, session_id: str):
    """질문 그룹 전체를 대상으로 결과분석을 실행합니다."""
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    detail = get_session_detail(session_id)
    if not detail or not detail.get("turns"):
        return JSONResponse({"error": "Not found"}, status_code=404)
    # 이미 pending 상태면 중복 요청 방지
    if any(t["analysis_status"] == "pending" for t in detail["turns"]):
        return {"success": False, "message": "이미 분석 중입니다."}
    # 마지막 턴 데이터로 분석 시작
    last = detail["turns"][-1]
    set_group_analysis_pending(session_id)
    asyncio.create_task(
        analyze_bad_feedback(
            last["id"],
            last["question"],
            last["answer"] or "",
            bool(last["is_no_answer"]),
            session_id=session_id,
            group_feedback=detail.get("group_feedback") or 0,
        )
    )
    return {"success": True}


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


@app.get("/admin/sessions")
async def admin_sessions(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    group_feedback: Optional[int] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
):
    """질문 그룹 단위 목록을 반환합니다."""
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return get_session_groups(
        page=page,
        page_size=page_size,
        group_feedback=group_feedback,
        date_from=date_from,
        date_to=date_to,
        q=q,
    )


@app.get("/admin/sessions/{session_id}")
async def admin_session_detail(request: Request, session_id: str):
    """질문 그룹 상세 (전체 transcript + 분석 결과)를 반환합니다."""
    if not _check_admin_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    detail = get_session_detail(session_id)
    if not detail:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return detail
