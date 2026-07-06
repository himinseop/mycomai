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


# ── 역할별 모델/프로바이더 런타임 스위치 (관리자 설정에서 변경 가능) ─────────
# app_settings 오버라이드 > .env 기본값. 재시작 없이 즉시 반영.
#   llm_model_<role>:    OpenAI 모델명 오버라이드 (openai provider일 때만 의미)
#   llm_provider_<role>: 역할별 provider ("openai" | "ollama", 빈 값 = 전역 LLM_PROVIDER)
# 역할: chat(답변 생성), rewrite(질문 재작성), summarize(요약·보조), insight(인사이트 API)

_ROLE_DEFAULTS = {
    "chat":      lambda: settings.OPENAI_CHAT_MODEL,        # RAG 답변 생성
    "summarize": lambda: settings.OPENAI_SUMMARIZE_MODEL,   # 요약·후속질문·안내 멘트
    "rewrite":   lambda: settings.QUERY_REWRITE_MODEL,      # 질문 재작성/해석
    "insight":   lambda: settings.INSIGHT_LLM_MODEL or settings.OPENAI_SUMMARIZE_MODEL,  # 인사이트 API
}

# openai provider일 때 역할별로 재사용할 인스턴스 (temperature 기본값 차이)
_OPENAI_BY_ROLE = {
    "chat": default_llm,
    "rewrite": default_llm,
    "summarize": summarizer_llm,
    "insight": summarizer_llm,
}

_ollama_llm: Optional[OpenAIProvider] = None


def _get_setting_safe(key: str) -> str:
    try:
        from company_llm_rag.history_store import get_setting
        return (get_setting(key, "") or "").strip()
    except Exception:
        return ""


def current_provider_name(role: str) -> str:
    """역할별 provider 이름 ("openai" | "ollama")."""
    return _get_setting_safe(f"llm_provider_{role}") or settings.LLM_PROVIDER


def resolve_llm(role: str):
    """역할별 (LLM 인스턴스, 모델명 오버라이드)를 반환합니다.

    모델명이 None이면 인스턴스의 기본 모델을 사용합니다 (ollama = OLLAMA_MODEL).
    """
    if current_provider_name(role) == "ollama":
        global _ollama_llm
        if _ollama_llm is None:
            _ollama_llm = OpenAIProvider(
                api_key="ollama",  # Ollama는 인증 불필요 (더미 키)
                base_url=settings.OLLAMA_BASE_URL,
                default_model=settings.OLLAMA_MODEL,
                default_temperature=0.3,
            )
        return _ollama_llm, None
    return _OPENAI_BY_ROLE[role], current_model(role)


def current_model(role: str) -> Optional[str]:
    """openai provider에서 역할별 사용할 모델명 (오버라이드 > .env 기본값)."""
    if current_provider_name(role) != "openai":
        return None
    return _get_setting_safe(f"llm_model_{role}") or _ROLE_DEFAULTS[role]()


def current_model_name(role: str) -> str:
    """표시/로깅용: 역할이 실제 사용할 모델명."""
    llm, model = resolve_llm(role)
    return model or llm.model_name
