"""
LLM 인스턴스 팩토리

앱 전체에서 사용하는 LLM 인스턴스를 한 곳에서 생성/관리합니다.
"""

from company_llm_rag.config import settings
from company_llm_rag.llm.openai_provider import OpenAIProvider

# 기본 LLM (답변 생성)
default_llm = OpenAIProvider()

# 경량 LLM (요약, 안내 멘트 등)
summarizer_llm = OpenAIProvider(
    default_model=settings.OPENAI_SUMMARIZE_MODEL,
    default_temperature=0.3,
)
