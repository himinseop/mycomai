"""
커스텀 예외 클래스

호출자가 에러 유형을 구분하여 처리할 수 있도록 계층적 예외를 정의합니다.
"""


class MycomaiError(Exception):
    """Mycomai RAG 시스템 기본 예외."""


class ConfigurationError(MycomaiError):
    """설정 오류 (필수 환경변수 누락 등)."""


class AuthenticationError(MycomaiError):
    """외부 서비스 인증 실패 (MSAL 토큰 획득 실패 등)."""


class ExtractionError(MycomaiError):
    """데이터 수집 중 발생한 오류."""

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        super().__init__(f"[{source}] {message}")


class RetrievalError(MycomaiError):
    """ChromaDB 검색 중 발생한 오류."""


class LLMError(MycomaiError):
    """LLM API 호출 중 발생한 오류."""
