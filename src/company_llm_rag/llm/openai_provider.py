"""
OpenAI LLMProvider 구현체

OpenAI Chat Completions API를 사용합니다.
"""

from typing import Dict, List, Optional

import openai

from company_llm_rag.config import settings
from company_llm_rag.exceptions import LLMError
from company_llm_rag.logger import get_logger
from company_llm_rag.llm.base import LLMProvider

logger = get_logger(__name__)


class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions API 기반 LLM 제공자."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
        default_temperature: Optional[float] = None,
    ) -> None:
        self._client = openai.OpenAI(api_key=api_key or settings.OPENAI_API_KEY)
        self._default_model = default_model or settings.OPENAI_CHAT_MODEL
        self._default_temperature = (
            default_temperature
            if default_temperature is not None
            else settings.OPENAI_TEMPERATURE
        )

    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        kwargs: Dict = {
            "model": model or self._default_model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self._default_temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        try:
            response = self._client.chat.completions.create(**kwargs)
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"OpenAI API error: {e}", exc_info=True)
            raise LLMError(str(e)) from e


# 앱 전체에서 공유하는 기본 인스턴스
default_llm = OpenAIProvider()
