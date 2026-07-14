"""
위키 직접 응답 (#58 Phase 2)

1위 검색 결과가 '승인된(approved)' 위키 페이지이고 충분히 우세하면,
답변 생성 LLM을 거치지 않고 페이지 원문을 직접 반환합니다.
Hub 직접 응답이 항상 먼저 검사되므로(호출 순서) Hub 우선순위가 보장됩니다.
draft 페이지는 컨텍스트 부스트만 (직접 반환 금지 — 검수 게이트).
"""

from typing import Dict, List, Optional

from company_llm_rag.wiki import wiki_store
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

# 1위 위키의 RRF 점수가 2위의 N배 이상이면 원문 직접 반환 (Hub와 동일 기준)
_WIKI_DIRECT_RRF_RATIO = 2.0


def _build_wiki_intro(question: str) -> str:
    from company_llm_rag.llm.factory import resolve_llm
    try:
        messages = [
            {"role": "system", "content": (
                "사용자 질문 주제에 대해 정리된 사내 위키 문서가 있습니다. "
                "어떤 내용의 문서인지 1문장으로 자연스럽게 안내하세요. "
                "답변 내용은 포함하지 말고, '정리된 위키 문서를 안내드립니다.'로 마무리하세요."
            )},
            {"role": "user", "content": question},
        ]
        _llm, _model = resolve_llm("summarize")
        intro = _llm.chat(messages, model=_model).strip()
    except Exception:
        intro = "관련 주제로 정리된 위키 문서를 안내드립니다."
    return intro + "\n\n---\n\n"


def try_wiki_direct_answer(retrieved_docs: List[Dict], question: str = "") -> Optional[str]:
    """approved 위키가 1위 + 우세하면 페이지 원문(기준일 표기 포함)을 반환합니다."""
    if not retrieved_docs:
        return None
    top = retrieved_docs[0]
    meta = top.get("metadata", {})
    if not meta.get("is_wiki"):
        return None
    top_rrf = top.get("_rrf", 0)
    second_rrf = retrieved_docs[1].get("_rrf", 0) if len(retrieved_docs) > 1 else 0
    if second_rrf > 0 and top_rrf / second_rrf < _WIKI_DIRECT_RRF_RATIO:
        return None
    page = wiki_store.get_page_by_wiki_id(meta.get("wiki_id"))
    if not page or page["status"] != wiki_store.STATUS_APPROVED:
        return None  # draft/disabled — 직접 반환 금지 (검수 게이트)

    intro = _build_wiki_intro(question or "")
    footer = (f"\n\n---\n_📖 사내 위키 · 기준일 {page['updated_at'][:10]} · "
              f"내용에 오류가 있으면 담당자에게 알려주세요._")
    logger.info(f"[Wiki] 직접 응답: {page['topic']} (id={page['id']})")
    return intro + f"## {page['title']}\n\n{page['content']}" + footer
