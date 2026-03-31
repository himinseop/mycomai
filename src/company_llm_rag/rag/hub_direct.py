"""
Knowledge Hub 직접 응답 판정 및 안내 멘트 생성

1위 검색 결과가 Knowledge Hub 문서이고 충분히 우세하면,
LLM을 거치지 않고 SQLite에서 답변 원문을 직접 반환합니다.
"""

from typing import Dict, List, Optional

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

# 1위 문서의 RRF 점수가 2위의 N배 이상이면 원문 직접 반환
_HUB_DIRECT_RRF_RATIO = 2.0

_hub_intro_llm = None


def _build_hub_intro(question: str) -> str:
    """LLM으로 Knowledge Hub 안내 멘트를 생성합니다."""
    global _hub_intro_llm
    if _hub_intro_llm is None:
        from company_llm_rag.llm.openai_provider import OpenAIProvider
        _hub_intro_llm = OpenAIProvider(
            default_model=settings.OPENAI_SUMMARIZE_MODEL,
            default_temperature=0.3,
        )
    try:
        messages = [
            {"role": "system", "content": (
                "사용자 질문과 유사한 기존 Q&A를 찾았습니다. "
                "해당 질문이 어떤 내용인지 1~2문장으로 자연스럽게 안내하세요. "
                "안내 문구만 작성하고, 답변 내용은 포함하지 마세요. "
                "반드시 '관련하여 유사한 질문의 답변을 안내드립니다.' 로 마무리하세요."
            )},
            {"role": "user", "content": question},
        ]
        intro = _hub_intro_llm.chat(messages).strip()
    except Exception:
        intro = "유사한 질문에 대한 답변이 있어 안내드립니다."
    return intro + "\n\n---\n\n"


def try_hub_direct_answer(retrieved_docs: List[Dict]) -> Optional[str]:
    """Knowledge Hub 문서가 1위이고 충분히 우세하면 SQLite에서 원문을 직접 반환합니다."""
    if not retrieved_docs or not settings.KNOWLEDGE_HUB_TEAM_NAME:
        return None
    top = retrieved_docs[0]
    meta = top.get('metadata', {})
    if not meta.get('is_hub_direct'):
        return None
    # RRF 점수 비교: 2위 대비 충분히 높을 때만
    top_rrf = top.get('_rrf', 0)
    second_rrf = retrieved_docs[1].get('_rrf', 0) if len(retrieved_docs) > 1 else 0
    if second_rrf > 0 and top_rrf / second_rrf < _HUB_DIRECT_RRF_RATIO:
        return None
    doc_id = meta.get('original_doc_id', '')
    if not doc_id:
        return None
    from company_llm_rag.history_store import hub_get_reply
    reply = hub_get_reply(doc_id)
    if not reply:
        return None
    # 안내 메시지: LLM이 질문을 자연스럽게 정리
    content_lines = top.get('content', '').strip()
    title = meta.get('title', '')
    if title and content_lines.startswith(title):
        content_lines = content_lines[len(title):].strip()
    intro = _build_hub_intro(content_lines)
    return intro + reply
