"""
LLM 인스턴스 팩토리

앱 전체에서 사용하는 LLM 인스턴스를 한 곳에서 생성/관리합니다.
LLM_PROVIDER 설정에 따라 OpenAI 또는 Ollama(OpenAI 호환 /v1)를 사용합니다 (#38).

주의: 임베딩은 항상 OpenAI 유지 (ChromaDB 인덱스가 1536d로 구축됨). 여기서 제공하는
것은 '생성 LLM'뿐입니다.
"""

from typing import Optional

from company_llm_rag.config import settings
from company_llm_rag.llm.openai_provider import OpenAIProvider


def _make_llm(
    default_model: Optional[str] = None,
    default_temperature: Optional[float] = None,
) -> OpenAIProvider:
    """LLM_PROVIDER 설정에 맞는 생성 LLM 제공자를 만듭니다."""
    if settings.LLM_PROVIDER == "ollama":
        return OpenAIProvider(
            api_key="ollama",  # Ollama는 인증 불필요 (더미 키)
            base_url=settings.OLLAMA_BASE_URL,
            default_model=settings.OLLAMA_MODEL,
            default_temperature=default_temperature,
        )
    return OpenAIProvider(
        default_model=default_model,
        default_temperature=default_temperature,
    )


# 기본 LLM (답변 생성)
default_llm = _make_llm()

# 경량 LLM (요약, 안내 멘트 등) — openai일 때는 요약 모델, ollama일 때는 OLLAMA_MODEL
summarizer_llm = _make_llm(
    default_model=settings.OPENAI_SUMMARIZE_MODEL,
    default_temperature=0.3,
)
