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


# ── 역할별 모델 런타임 스위치 (관리자 설정에서 변경 가능) ────────────────────
# app_settings의 llm_model_<role> 오버라이드 > .env 기본값. 재시작 없이 즉시 반영.

_ROLE_DEFAULTS = {
    "chat":      lambda: settings.OPENAI_CHAT_MODEL,        # RAG 답변 생성
    "summarize": lambda: settings.OPENAI_SUMMARIZE_MODEL,   # 요약·후속질문·안내 멘트
    "rewrite":   lambda: settings.QUERY_REWRITE_MODEL,      # 질문 재작성/해석
    "insight":   lambda: settings.INSIGHT_LLM_MODEL or settings.OPENAI_SUMMARIZE_MODEL,  # 인사이트 API
}


def current_model(role: str) -> Optional[str]:
    """역할별로 지금 사용할 모델명을 반환합니다. None이면 provider 기본 모델 사용.

    ollama 등 비-OpenAI provider에서는 OpenAI 모델명 오버라이드가 무의미하므로
    None을 반환해 provider 기본 모델을 쓰게 합니다.
    """
    if settings.LLM_PROVIDER != "openai":
        return None
    try:
        from company_llm_rag.history_store import get_setting
        override = (get_setting(f"llm_model_{role}", "") or "").strip()
    except Exception:
        override = ""
    return override or _ROLE_DEFAULTS[role]()
