"""
인사이트 API 라우터 — POST /api/v1/insights (단일 엔드포인트)

도메인별 경로 없이, 요청 데이터(records 구조)와 질문(question)을 근거로
서버가 적합한 도메인 프롬프트를 자동 선택합니다 (classifier.py).
공통 파이프라인: 인증 → rate limit → 도메인 선택 → scope → 통계 선계산
→ LLM 해석(1회 재시도) → 구조화 응답. 인증된 호출은 모두 이력 기록.
"""

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from company_llm_rag.config import settings
from company_llm_rag.insight_api.auth import ensure_scope, require_client
from company_llm_rag.insight_api.classifier import classify_domain
from company_llm_rag.insight_api.domains import DOMAIN_REGISTRY
from company_llm_rag.insight_api.ratelimit import check_rate_limit
from company_llm_rag.insight_api.store import log_call
from company_llm_rag.llm.factory import current_model_name, resolve_llm
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/insights", tags=["insights"])

MAX_PAYLOAD_BYTES = 5 * 1024 * 1024  # 5MB
MAX_RECORDS = 10_000


class UnifiedInsightRequest(BaseModel):
    """단일 엔드포인트 요청 — records 스키마는 도메인 선택 후 도메인 모델로 검증."""
    question: Optional[str] = Field(None, max_length=1000, description="무엇이 궁금한지 (자연어)")
    domain: Optional[str] = Field(None, description="도메인 명시 지정 (생략 시 자동 선택)")
    period: Optional[Dict[str, Any]] = None       # 생략 시 records 날짜로 추론
    compare_period: Optional[Dict[str, Any]] = None
    records: List[Dict[str, Any]] = Field(min_length=1, max_length=MAX_RECORDS)
    options: Optional[Dict[str, Any]] = None


def _insight_model() -> str:
    return current_model_name("insight")


def _call_llm(messages) -> str:
    """LLM 호출 (테스트에서 monkeypatch 지점). 실패 시 1회 재시도."""
    llm, model = resolve_llm("insight")
    try:
        return llm.chat(messages, model=model, temperature=0.2, max_tokens=1500)
    except Exception as e:
        logger.warning(f"[InsightAPI] LLM 1차 실패, 재시도: {e}")
        return llm.chat(messages, model=model, temperature=0.2, max_tokens=1500)


def _infer_period(records: List[Dict[str, Any]]) -> Dict[str, str]:
    """period 미지정 시 records의 date 최소~최대로 추론 (ISO 문자열은 사전순=시간순)."""
    dates = sorted(
        str(r["date"])[:10] for r in records
        if r.get("date") is not None
    )
    if not dates:
        raise ValueError("period is missing and records have no 'date' field")
    return {"from": dates[0], "to": dates[-1]}


@router.get("/domains")
async def list_domains(client: Dict = Depends(require_client)):
    """클라이언트가 사용 가능한 도메인 목록 (참고용 — 호출 시 지정은 선택사항)."""
    scopes = client.get("scopes", [])
    allowed = [d for d in DOMAIN_REGISTRY if "*" in scopes or d in scopes]
    return {"domains": [
        {"name": d, "description": DOMAIN_REGISTRY[d].description}
        for d in allowed
    ]}


@router.post("")
async def create_insight(request: Request, client: Dict = Depends(require_client)):
    t0 = time.monotonic()
    request_id = "req-" + uuid.uuid4().hex[:12]
    loop = asyncio.get_event_loop()

    def _log(status: int, domain: str = "-", req_summary=None, resp_summary=None,
             error=None, selection: Optional[str] = None):
        if req_summary is not None and selection:
            req_summary = {**req_summary, "domain_selection": selection}
        log_call(
            request_id=request_id, client_id=client["id"], domain=domain,
            status=status, request_summary=req_summary, response_summary=resp_summary,
            model=_insight_model(),
            latency_ms=int((time.monotonic() - t0) * 1000), error=error,
        )

    if not check_rate_limit(client):
        _log(429, error="rate limit exceeded")
        raise HTTPException(status_code=429, detail="rate limit exceeded")

    body = await request.body()
    if len(body) > MAX_PAYLOAD_BYTES:
        _log(422, error=f"payload too large ({len(body)} bytes)")
        raise HTTPException(status_code=422, detail="payload exceeds 5MB limit")

    try:
        unified = UnifiedInsightRequest.model_validate_json(body)
    except ValidationError as e:
        errors = e.errors(include_url=False, include_context=False, include_input=False)
        _log(422, error=f"validation: {errors[:3]}")
        raise HTTPException(status_code=422, detail=errors)

    # ── 도메인 선택: 명시 지정 > 데이터 구조 감지 > LLM 분류 ──────────────
    if unified.domain:
        if unified.domain not in DOMAIN_REGISTRY:
            _log(422, error=f"unknown domain '{unified.domain}'")
            raise HTTPException(
                status_code=422,
                detail=f"unknown domain '{unified.domain}' "
                       f"(available: {sorted(DOMAIN_REGISTRY)})",
            )
        domain_name, selection = unified.domain, "explicit"
    else:
        try:
            domain_name, selection = await loop.run_in_executor(
                None, classify_domain,
                unified.records, unified.question, DOMAIN_REGISTRY,
            )
        except Exception as e:
            _log(422, error=f"domain classification failed: {e}")
            raise HTTPException(
                status_code=422,
                detail="도메인을 판단할 수 없습니다. 'domain' 필드로 명시해 주세요. "
                       f"(available: {sorted(DOMAIN_REGISTRY)})",
            )
    domain = DOMAIN_REGISTRY[domain_name]

    try:
        ensure_scope(client, domain_name)
    except HTTPException as e:
        _log(e.status_code, domain=domain_name, error=e.detail, selection=selection)
        raise

    # ── 도메인 모델 검증 (period 미지정 시 records 날짜로 추론) ───────────
    payload: Dict[str, Any] = {"records": unified.records}
    try:
        payload["period"] = unified.period or _infer_period(unified.records)
    except ValueError as e:
        _log(422, domain=domain_name, error=str(e), selection=selection)
        raise HTTPException(status_code=422, detail=str(e))
    if unified.compare_period:
        payload["compare_period"] = unified.compare_period
    if unified.options:
        payload["options"] = unified.options

    try:
        req = domain.request_model.model_validate(payload)
    except ValidationError as e:
        errors = e.errors(include_url=False, include_context=False, include_input=False)
        _log(422, domain=domain_name, error=f"validation: {errors[:3]}", selection=selection)
        raise HTTPException(status_code=422, detail=errors)

    # 결정적 통계 선계산 — 수치는 여기서 확정 (LLM은 해석만)
    try:
        stats = domain.preprocess(req)
    except ValueError as e:
        _log(422, domain=domain_name, error=str(e), selection=selection)
        raise HTTPException(status_code=422, detail=str(e))

    req_summary = domain.request_summary(req, stats)
    if unified.question:
        req_summary["question"] = unified.question[:200]

    messages = domain.build_messages(req, stats, question=unified.question or "")
    try:
        raw = await loop.run_in_executor(None, _call_llm, messages)
    except Exception as e:
        logger.error(f"[InsightAPI] LLM 호출 실패: {e}")
        _log(502, domain=domain_name, req_summary=req_summary, error=str(e),
             selection=selection)
        raise HTTPException(status_code=502, detail="llm call failed")

    parsed = domain.parse_response(raw)
    latency_ms = int((time.monotonic() - t0) * 1000)

    _log(200, domain=domain_name, req_summary=req_summary, resp_summary={
        "summary_head": parsed["summary"][:200],
        "highlights": len(parsed["highlights"]),
        "anomalies": len(parsed["anomalies"]),
    }, selection=selection)
    logger.info(
        f"[InsightAPI] {client['name']} → {domain_name}({selection}) OK "
        f"({latency_ms}ms, highlights={len(parsed['highlights'])})"
    )

    return {
        "domain": domain_name,
        "domain_selection": selection,   # explicit | structure | llm
        "request_id": request_id,
        "summary": parsed["summary"],
        "highlights": parsed["highlights"],
        "anomalies": parsed["anomalies"],
        "stats": domain.postprocess_stats(stats),
        "meta": {"model": _insight_model(), "latency_ms": latency_ms},
    }
