"""
LLMProvider 추상 인터페이스

rag_system, teams_sender 등이 이 인터페이스에만 의존하도록 하여
LLM 교체(OpenAI → Claude, Ollama 등) 시 Provider 구현체만 변경하면 됩니다.
"""

from abc import ABC, abstractmethod
from typing import Dict, Generator, List, Optional


class LLMProvider(ABC):
    """LLM 제공자 추상 인터페이스."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """현재 사용 중인 모델명을 반환합니다."""

    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        채팅 완성 요청을 보내고 응답 텍스트를 반환합니다.

        Args:
            messages:    [{"role": "system"|"user"|"assistant", "content": "..."}, ...]
            model:       사용할 모델명 (None이면 기본값 사용)
            temperature: 생성 온도 (None이면 기본값 사용)
            max_tokens:  최대 토큰 수 (None이면 제한 없음)

        Returns:
            LLM 응답 텍스트

        Raises:
            LLMError: 호출 실패 시
        """

    @abstractmethod
    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        """
        채팅 완성을 스트리밍으로 반환합니다.

        Yields:
            LLM 응답 토큰 문자열
        """
