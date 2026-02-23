"""
Configuration 모듈 테스트
"""

import os
import pytest
from company_llm_rag.config import Settings


class TestSettings:
    """Settings 클래스 테스트"""

    def test_settings_defaults(self):
        """기본값이 올바르게 설정되는지 테스트"""
        settings = Settings()

        # 기본값 확인
        assert settings.CHUNK_SIZE == 100
        assert settings.CHUNK_OVERLAP == 50
        assert settings.RETRIEVAL_TOP_K == 3
        assert settings.LOG_LEVEL == "INFO"
        assert settings.OPENAI_CHAT_MODEL == "gpt-4o"
        assert settings.OPENAI_EMBEDDING_MODEL == "text-embedding-3-small"
        assert settings.OPENAI_TEMPERATURE == 0.7

    def test_auth_header_jira(self):
        """Jira 인증 헤더 생성 테스트"""
        settings = Settings()
        settings.JIRA_EMAIL = "test@example.com"
        settings.JIRA_API_TOKEN = "test-token"

        headers = settings.get_auth_header("jira")

        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Basic ")
        assert headers["Accept"] == "application/json"

    def test_auth_header_confluence(self):
        """Confluence 인증 헤더 생성 테스트"""
        settings = Settings()
        settings.CONFLUENCE_EMAIL = "test@example.com"
        settings.CONFLUENCE_API_TOKEN = "test-token"

        headers = settings.get_auth_header("confluence")

        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Basic ")
        assert headers["Accept"] == "application/json"

    def test_auth_header_unknown_service(self):
        """알 수 없는 서비스에 대한 예외 처리 테스트"""
        settings = Settings()

        with pytest.raises(ValueError, match="Unknown service"):
            settings.get_auth_header("unknown")

    def test_validate_missing_openai_key(self):
        """OPENAI_API_KEY 누락 시 검증 실패 테스트"""
        settings = Settings()
        settings.OPENAI_API_KEY = ""

        with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
            settings.validate()
