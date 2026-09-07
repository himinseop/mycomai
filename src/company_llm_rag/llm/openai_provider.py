"""
OpenAI LLMProvider 구현체

OpenAI Chat Completions API를 사용합니다.
"""

from typing import Dict, List, Optional

import re
import time

import openai

from company_llm_rag.config import settings
from company_llm_rag.exceptions import LLMError
from company_llm_rag.logger import get_logger
from company_llm_rag.llm.base import LLMProvider

# 속도 제한(429)·일시 오류 재시도 — OpenAI SDK 기본 재시도(짧은 백오프)로는
# "try again in 19s" 류 TPM 한도를 넘기지 못해 답변 실패로 이어짐 (프랜차이즈 관점 테스트에서 확인).
_RETRY_ATTEMPTS = max(0, int(getattr(settings, "OPENAI_RATE_LIMIT_RETRIES", 3)))
_RETRY_MAX_WAIT_SEC = 30.0
_RETRY_AFTER_RE = re.compile(r"try again in\s*([0-9.]+)\s*(ms|s)", re.IGNORECASE)


def _retry_wait_seconds(exc: Exception, attempt: int) -> float:
    """429 메시지의 'try again in Xs' 또는 Retry-After 헤더를 우선, 없으면 지수 백오프."""
    wait = None
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers:
        ra = headers.get("retry-after")
        if ra:
            try:
                wait = float(ra)
            except ValueError:
                wait = None
    if wait is None:
        m = _RETRY_AFTER_RE.search(str(exc))
        if m:
            wait = float(m.group(1)) / (1000.0 if m.group(2).lower() == "ms" else 1.0)
    if wait is None:
        wait = 2.0 * (2 ** attempt)
    return min(max(wait + 0.5, 1.0), _RETRY_MAX_WAIT_SEC)


def _is_retryable(exc: Exception) -> bool:
    return isinstance(exc, (openai.RateLimitError, openai.APIConnectionError,
                            openai.APITimeoutError, openai.InternalServerError))


def _call_with_retry(fn, what: str):
    """fn()을 호출하고 429·연결·5xx 오류면 대기 후 최대 _RETRY_ATTEMPTS회 재시도."""
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as e:
            if not _is_retryable(e) or attempt >= _RETRY_ATTEMPTS:
                raise
            wait = _retry_wait_seconds(e, attempt)
            attempt += 1
            logger.warning(
                f"OpenAI {what} 일시 오류({type(e).__name__}) — {wait:.1f}s 후 재시도 {attempt}/{_RETRY_ATTEMPTS}")
            time.sleep(wait)

logger = get_logger(__name__)


class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions API 기반 LLM 제공자."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
        default_temperature: Optional[float] = None,
        base_url: Optional[str] = None,
    ) -> None:
        # base_url 지정 시 OpenAI 호환 서버(예: Ollama /v1)로 라우팅 (#38)
        self._client = openai.OpenAI(
            api_key=api_key or settings.OPENAI_API_KEY,
            base_url=base_url,
        )
        self._default_model = default_model or settings.OPENAI_CHAT_MODEL
        self._default_temperature = (
            default_temperature
            if default_temperature is not None
            else settings.OPENAI_TEMPERATURE
        )
        # temperature 미지원 모델 여부 캐시 (한 번 실패하면 이후 요청부터 생략)
        self._temperature_unsupported = False

    @property
    def model_name(self) -> str:
        return self._default_model

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
        }
        # temperature 미지원으로 확인된 모델은 처음부터 생략
        if not self._temperature_unsupported:
            kwargs["temperature"] = temperature if temperature is not None else self._default_temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        try:
            response = _call_with_retry(lambda: self._client.chat.completions.create(**kwargs), "chat")
            return response.choices[0].message.content
        except openai.BadRequestError as e:
            # 일부 모델(gpt-5 등)은 temperature 파라미터를 지원하지 않음 → 제외 후 재시도
            if "temperature" in str(e) and "temperature" in kwargs:
                logger.warning(f"모델이 temperature를 지원하지 않음 — 이후 요청에서 temperature 생략: {e}")
                self._temperature_unsupported = True
                kwargs.pop("temperature")
                try:
                    response = self._client.chat.completions.create(**kwargs)
                    return response.choices[0].message.content
                except Exception as e2:
                    logger.error(f"OpenAI API error: {e2}", exc_info=True)
                    raise LLMError(str(e2)) from e2
            logger.error(f"OpenAI API error: {e}", exc_info=True)
            raise LLMError(str(e)) from e
        except Exception as e:
            logger.error(f"OpenAI API error: {e}", exc_info=True)
            raise LLMError(str(e)) from e

    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """스트리밍 LLM 응답을 텍스트 청크로 yield합니다."""
        kwargs: Dict = {
            "model": model or self._default_model,
            "messages": messages,
            "stream": True,
        }
        if not self._temperature_unsupported:
            kwargs["temperature"] = temperature if temperature is not None else self._default_temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        def _iter_stream(kw):
            # 429는 스트림 생성(create) 시점에 발생 — 토큰을 내보내기 전이므로 안전하게 재시도
            stream = _call_with_retry(lambda: self._client.chat.completions.create(**kw), "stream")
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta

        try:
            yield from _iter_stream(kwargs)
        except openai.BadRequestError as e:
            if "temperature" in str(e) and "temperature" in kwargs:
                logger.warning(f"모델이 temperature를 지원하지 않음 (스트리밍) — 이후 요청에서 temperature 생략: {e}")
                self._temperature_unsupported = True
                kwargs.pop("temperature")
                try:
                    yield from _iter_stream(kwargs)
                except Exception as e2:
                    logger.error(f"OpenAI stream error: {e2}", exc_info=True)
                    raise LLMError(str(e2)) from e2
            else:
                logger.error(f"OpenAI stream error: {e}", exc_info=True)
                raise LLMError(str(e)) from e
        except Exception as e:
            logger.error(f"OpenAI stream error: {e}", exc_info=True)
            raise LLMError(str(e)) from e

