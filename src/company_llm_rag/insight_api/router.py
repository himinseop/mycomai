"""
인사이트 API 라우터 — POST /api/v1/insights/{domain}

공통 파이프라인: 인증(scope) → 입력 검증 → 통계 선계산 → LLM 해석(1회 재시도)
→ 구조화 응답. 모든 인증된 호출은 api_call_history에 기록됩니다.
"""

import asyncio
import time
import uuid
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from company_llm_rag.config import settings
from company_llm_rag.insight_api.auth import ensure_scope, require_client
from company_llm_rag.insight_api.domains import DOMAIN_REGISTRY
from company_llm_rag.insight_api.ratelimit import check_rate_limit
from company_llm_rag.insight_api.store import log_call
from company_llm_rag.llm.factory import summarizer_llm
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/insights", tags=["insights"])

MAX_PAYLOAD_BYTES = 5 * 1024 * 1024  # 5MB


def _insight_model() -> str:
    return settings.INSIGHT_LLM_MODEL or settings.OPENAI_SUMMARIZE_MODEL


def _call_llm(messages) -> str:
    """LLM 호출 (테스트에서 monkeypatch 지점). 실패 시 1회 재시도."""
    try:
        return summarizer_llm.chat(
            messages, model=_insight_model(), temperature=0.2, max_tokens=1500
        )
    except Exception as e:
        logger.warning(f"[InsightAPI] LLM 1차 실패, 재시도: {e}")
        return summarizer_llm.chat(
            messages, model=_insight_model(), temperature=0.2, max_tokens=1500
        )


@router.get("/domains")
async def list_domains(client: Dict = Depends(require_client)):
    """클라이언트가 사용 가능한 도메인 목록."""
    return {"domains": [d for d in DOMAIN_REGISTRY if d in client.get("scopes", [])]}


@router.post("/{domain_name}")
async def create_insight(
    domain_name: str,
    request: Request,
    client: Dict = Depends(require_client),
):
    t0 = time.monotonic()
    request_id = "req-" + uuid.uuid4().hex[:12]

    domain = DOMAIN_REGISTRY.get(domain_name)
    if domain is None:
        raise HTTPException(status_code=404, detail=f"unknown domain '{domain_name}'")

    def _log(status: int, req_summary=None, resp_summary=None, error=None):
        log_call(
            request_id=request_id, client_id=client["id"], domain=domain_name,
            status=status, request_summary=req_summary, response_summary=resp_summary,
            model=_insight_model(),
            latency_ms=int((time.monotonic() - t0) * 1000), error=error,
        )

    try:
        ensure_scope(client, domain_name)
    except HTTPException as e:
        _log(e.status_code, error=e.detail)
        raise

    if not check_rate_limit(client):
        _log(429, error="rate limit exceeded")
        raise HTTPException(status_code=429, detail="rate limit exceeded")

    body = await request.body()
    if len(body) > MAX_PAYLOAD_BYTES:
        _log(422, error=f"payload too large ({len(body)} bytes)")
        raise HTTPException(status_code=422, detail="payload exceeds 5MB limit")

    try:
        req = domain.request_model.model_validate_json(body)
    except ValidationError as e:
        # ctx의 예외 객체 등 직렬화 불가 항목 제외
        errors = e.errors(include_url=False, include_context=False, include_input=False)
        _log(422, error=f"validation: {errors[:3]}")
        raise HTTPException(status_code=422, detail=errors)

    # 결정적 통계 선계산 — 수치는 여기서 확정 (LLM은 해석만)
    try:
        stats = domain.preprocess(req)
    except ValueError as e:
        _log(422, req_summary=None, error=str(e))
        raise HTTPException(status_code=422, detail=str(e))

    req_summary = domain.request_summary(req, stats)

    messages = domain.build_messages(req, stats)
    loop = asyncio.get_event_loop()
    try:
        raw = await loop.run_in_executor(None, _call_llm, messages)
    except Exception as e:
        logger.error(f"[InsightAPI] LLM 호출 실패: {e}")
        _log(502, req_summary=req_summary, error=str(e))
        raise HTTPException(status_code=502, detail="llm call failed")

    parsed = domain.parse_response(raw)
    latency_ms = int((time.monotonic() - t0) * 1000)

    _log(200, req_summary=req_summary, resp_summary={
        "summary_head": parsed["summary"][:200],
        "highlights": len(parsed["highlights"]),
        "anomalies": len(parsed["anomalies"]),
    })
    logger.info(
        f"[InsightAPI] {client['name']} → {domain_name} OK "
        f"({latency_ms}ms, highlights={len(parsed['highlights'])})"
    )

    return {
        "domain": domain_name,
        "request_id": request_id,
        "summary": parsed["summary"],
        "highlights": parsed["highlights"],
        "anomalies": parsed["anomalies"],
        "stats": domain.postprocess_stats(stats),  # LLM 전용 필드(샘플 등) 제거
        "meta": {"model": _insight_model(), "latency_ms": latency_ms},
    }
