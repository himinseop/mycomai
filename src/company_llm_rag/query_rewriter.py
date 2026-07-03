"""
질문 재작성(Query Rewriting) 모듈 (#52)

사용자 질문(구어체·오타·약어·불완전)을 LLM으로 검색 친화적 문장으로 재작성하고
핵심 키워드를 추출합니다. 재작성 결과는 원문과 '함께' 검색에 사용됩니다
(retrieve_documents(extra_queries=..., extra_keywords=...)).

- QUERY_REWRITE_ENABLED=false(기본)이면 아무 동작 없이 원문만 반환 → 안전한 롤아웃
- 재작성 실패/오류 시 원문 fallback
- 단일 턴 질문은 캐시하여 동일 질문 재호출 비용 절감
"""

import json
from typing import Dict, List, Optional

from company_llm_rag.config import settings
from company_llm_rag.llm.factory import default_llm
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_cache: Dict[str, dict] = {}   # 단일 턴 질문 → {"rewritten", "keywords"}
_CACHE_MAX = 512

_SYSTEM_PROMPT = """당신은 사내 지식베이스(Jira/Confluence/SharePoint/Teams) 검색을 돕는 질의 재작성기입니다.
사용자 질문을 검색에 유리한 명확한 한국어 검색문 1개와 핵심 키워드로 변환하세요.

규칙:
- 오타/약어/구어체를 표준 용어로 보정합니다 (예: "위메포"→"위메프오", "정산 언제"→"정산 주기").
- 이전 대화 맥락이 있으면 반영해, 그 자체로 이해되는 독립적 검색문으로 만듭니다.
- 원 질문의 의도를 왜곡하거나 없는 조건을 추가하지 마세요.
- 키워드는 검색에 유효한 명사 위주 2~4개.
- understanding: 사용자에게 보여줄 자연스러운 한 문장. "질문을 이렇게 이해했고 지금 찾아보겠다"는
  뜻을 대화체로 담습니다. 예: "위메프오 정산 주기에 대한 질문이시군요. 관련 자료를 찾아볼게요."
  (질문을 되짚되 답을 미리 말하지 말고, 검색을 시작한다는 뉘앙스로 마무리)

반드시 아래 JSON 한 줄만 출력하세요 (설명·코드펜스 금지):
{"understanding": "<사용자에게 보여줄 자연스러운 확인 문장>", "rewritten": "<검색문>", "keywords": ["키워드1", "키워드2"]}"""


def _parse_json(raw: str) -> dict:
    """LLM 출력에서 JSON 객체를 관대하게 파싱합니다."""
    if not raw:
        return {}
    text = raw.strip()
    # 코드펜스 제거
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    # 첫 '{' ~ 마지막 '}' 구간 추출
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return {}
    return {}


def rewrite_query(
    question: str,
    conversation_history: Optional[List[Dict]] = None,
) -> Dict[str, object]:
    """
    질문을 검색 친화적으로 재작성합니다.

    Returns:
        {"rewritten": str, "keywords": List[str], "understanding": str}
        - 비활성/실패 시 {"rewritten": <원문>, "keywords": [], "understanding": ""}
    """
    q = (question or "").strip()
    if not settings.QUERY_REWRITE_ENABLED or not q:
        return {"rewritten": q, "keywords": [], "understanding": ""}

    is_single_turn = not conversation_history
    if is_single_turn and q in _cache:
        return _cache[q]

    messages: List[Dict[str, str]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    if conversation_history:
        # 최근 2턴(user/assistant 4개)만 맥락으로 사용
        messages.extend(conversation_history[-4:])
    messages.append({"role": "user", "content": f"질문: {q}"})

    try:
        raw = default_llm.chat(
            messages,
            model=settings.QUERY_REWRITE_MODEL,
            temperature=0.0,
            max_tokens=250,
        )
        data = _parse_json(raw)
        rewritten = (data.get("rewritten") or "").strip() or q
        keywords = [k.strip() for k in (data.get("keywords") or [])
                    if isinstance(k, str) and k.strip()][:4]
        understanding = (data.get("understanding") or "").strip()
        result = {"rewritten": rewritten, "keywords": keywords, "understanding": understanding}
    except Exception as e:
        logger.warning(f"[QueryRewrite] 재작성 실패, 원문 사용: {e}")
        return {"rewritten": q, "keywords": [], "understanding": ""}

    if is_single_turn and len(_cache) < _CACHE_MAX:
        _cache[q] = result
    if result["rewritten"] != q or result["keywords"]:
        logger.info(f"[QueryRewrite] '{q}' → '{result['rewritten']}' | kw={result['keywords']}")
    return result
