"""
Reranker 인스턴스 팩토리

환경변수 기반으로 적절한 RerankerProvider를 생성합니다.
RERANKER_ENABLED=false이면 None을 반환하여 기존 파이프라인 유지.
"""

from typing import Optional

from company_llm_rag.config import settings
from company_llm_rag.reranker.base import RerankerProvider

_instance: Optional[RerankerProvider] = None


def get_reranker() -> Optional[RerankerProvider]:
    """환경변수 기반으로 reranker 인스턴스를 반환합니다. 비활성화 시 None."""
    global _instance
    if not settings.RERANKER_ENABLED:
        return None
    if _instance is None:
        from company_llm_rag.reranker.bge_provider import BGEReranker
        _instance = BGEReranker(model_name=settings.RERANKER_MODEL)
    return _instance
