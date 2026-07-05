"""
도메인 자동 선택기 (#56 개선)

요청 데이터(records 필드 구조)와 질문(question)을 근거로 어떤 도메인
프롬프트로 분석할지 서버가 선택합니다.

1단계 (결정적): 도메인별 signature_fields 커버리지 — 명확히 하나면 즉시 선택
2단계 (LLM):    구조가 애매하거나 복수 후보면 경량 LLM이 질문·필드·샘플로 판단
"""

import json
from typing import Dict, List, Optional, Tuple

from company_llm_rag.config import settings
from company_llm_rag.insight_api.domains.base import parse_llm_json
from company_llm_rag.llm.factory import summarizer_llm
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_SAMPLE_N = 50           # 구조 감지 표본 행수
_STRUCTURE_THRESHOLD = 0.8   # signature 필드 커버리지 기준
_LLM_SAMPLE_ROWS = 2     # LLM 분류에 보여줄 샘플 행수
_LLM_SAMPLE_CHARS = 300  # 샘플 행 직렬화 길이 상한


def _field_coverage(records: List[Dict], fields: set) -> float:
    """표본 행 중 signature 필드를 모두 가진 행의 비율."""
    sample = records[:_SAMPLE_N]
    if not sample or not fields:
        return 0.0
    hit = sum(1 for r in sample
              if all(f in r and r[f] is not None for f in fields))
    return hit / len(sample)


def _call_llm(messages: List[Dict[str, str]]) -> str:
    """LLM 분류 호출 (테스트 monkeypatch 지점)."""
    return summarizer_llm.chat(
        messages,
        model=settings.INSIGHT_LLM_MODEL or settings.OPENAI_SUMMARIZE_MODEL,
        temperature=0.0,
        max_tokens=100,
    )


def _llm_classify(
    records: List[Dict],
    question: Optional[str],
    registry: Dict,
    candidates: List[str],
) -> str:
    domain_lines = "\n".join(
        f"- {name}: {registry[name].description}" for name in candidates
    )
    fields = sorted({k for r in records[:_SAMPLE_N] for k in r.keys()})
    samples = [
        json.dumps(r, ensure_ascii=False, default=str)[:_LLM_SAMPLE_CHARS]
        for r in records[:_LLM_SAMPLE_ROWS]
    ]
    system = (
        "당신은 분석 요청 라우터입니다. 요청 데이터와 질문을 보고 가장 적합한 분석 도메인을 "
        "하나 선택하세요.\n\n[도메인 목록]\n" + domain_lines +
        '\n\n반드시 JSON 한 줄만 출력: {"domain": "<이름>"}'
    )
    user = (
        f"[질문] {question or '(없음)'}\n"
        f"[데이터 필드] {', '.join(fields)}\n"
        f"[샘플 행]\n" + "\n".join(samples)
    )
    raw = _call_llm([{"role": "system", "content": system},
                     {"role": "user", "content": user}])
    name = (parse_llm_json(raw).get("domain") or "").strip()
    if name not in registry:
        raise ValueError(f"domain classification failed (got: {name!r})")
    return name


def classify_domain(
    records: List[Dict],
    question: Optional[str],
    registry: Dict,
) -> Tuple[str, str]:
    """
    도메인을 선택합니다.

    Returns:
        (도메인명, 선택 방법 "structure" | "llm")
    Raises:
        ValueError: 판단 불가 (호출측에서 422 처리, 'domain' 명시 유도)
    """
    scores = {
        name: _field_coverage(records, d.signature_fields)
        for name, d in registry.items()
    }
    strong = [n for n, s in scores.items() if s >= _STRUCTURE_THRESHOLD]
    if len(strong) == 1:
        logger.info(f"[InsightAPI] 도메인 구조 감지: {strong[0]} (coverage={scores[strong[0]]:.2f})")
        return strong[0], "structure"

    # 후보 0개(미지의 구조) 또는 복수(혼합 필드) → LLM 판단
    candidates = strong or list(registry.keys())
    name = _llm_classify(records, question, registry, candidates)
    logger.info(f"[InsightAPI] 도메인 LLM 분류: {name} (후보={candidates})")
    return name, "llm"
