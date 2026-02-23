"""
중앙화된 설정 관리 모듈

모든 환경변수와 설정을 여기서 관리합니다.
"""

import os
from typing import Optional, List
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()


class Settings:
    """애플리케이션 전역 설정"""

    # OpenAI 설정
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_EMBEDDING_MODEL: str = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    OPENAI_CHAT_MODEL: str = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o")
    OPENAI_TEMPERATURE: float = float(os.getenv("OPENAI_TEMPERATURE", "0.7"))

    # ChromaDB 설정
    CHROMA_DB_PATH: str = os.getenv("CHROMA_DB_PATH", "./chroma_db")
    COLLECTION_NAME: str = os.getenv("COLLECTION_NAME", "company_llm_rag_collection")

    # Jira 설정
    JIRA_BASE_URL: str = os.getenv("JIRA_BASE_URL", "")
    JIRA_API_TOKEN: str = os.getenv("JIRA_API_TOKEN", "")
    JIRA_EMAIL: str = os.getenv("JIRA_EMAIL", "")
    JIRA_PROJECT_KEYS: List[str] = [
        k.strip() for k in os.getenv("JIRA_PROJECT_KEY", "").split(",") if k.strip()
    ]

    # Confluence 설정
    CONFLUENCE_BASE_URL: str = os.getenv("CONFLUENCE_BASE_URL", "")
    CONFLUENCE_API_TOKEN: str = os.getenv("CONFLUENCE_API_TOKEN", "")
    CONFLUENCE_EMAIL: str = os.getenv("CONFLUENCE_EMAIL", "")
    CONFLUENCE_SPACE_KEYS: List[str] = [
        k.strip() for k in os.getenv("CONFLUENCE_SPACE_KEY", "").split(",") if k.strip()
    ]

    # Microsoft 365 설정
    TENANT_ID: str = os.getenv("TENANT_ID", "")
    CLIENT_ID: str = os.getenv("CLIENT_ID", "")
    CLIENT_SECRET: str = os.getenv("CLIENT_SECRET", "")
    SHAREPOINT_SITE_NAME: str = os.getenv("SHAREPOINT_SITE_NAME", "")
    TEAMS_GROUP_NAME: str = os.getenv("TEAMS_GROUP_NAME", "")

    # 데이터 수집 설정
    LOOKBACK_DAYS: Optional[int] = (
        int(os.getenv("LOOKBACK_DAYS")) if os.getenv("LOOKBACK_DAYS") else None
    )

    # RAG 설정
    CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "100"))
    CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "50"))
    RETRIEVAL_TOP_K: int = int(os.getenv("RETRIEVAL_TOP_K", "3"))

    # 페이지네이션 설정
    JIRA_MAX_RESULTS: int = int(os.getenv("JIRA_MAX_RESULTS", "50"))
    CONFLUENCE_PAGE_LIMIT: int = int(os.getenv("CONFLUENCE_PAGE_LIMIT", "25"))

    def validate(self) -> None:
        """필수 설정값 검증"""
        errors = []

        if not self.OPENAI_API_KEY:
            errors.append("OPENAI_API_KEY is required")

        # Jira 설정 검증 (Jira를 사용하는 경우)
        if self.JIRA_PROJECT_KEYS:
            if not self.JIRA_BASE_URL:
                errors.append("JIRA_BASE_URL is required when using Jira")
            if not self.JIRA_API_TOKEN:
                errors.append("JIRA_API_TOKEN is required when using Jira")
            if not self.JIRA_EMAIL:
                errors.append("JIRA_EMAIL is required when using Jira")

        # Confluence 설정 검증
        if self.CONFLUENCE_SPACE_KEYS:
            if not self.CONFLUENCE_BASE_URL:
                errors.append("CONFLUENCE_BASE_URL is required when using Confluence")
            if not self.CONFLUENCE_API_TOKEN:
                errors.append("CONFLUENCE_API_TOKEN is required when using Confluence")
            if not self.CONFLUENCE_EMAIL:
                errors.append("CONFLUENCE_EMAIL is required when using Confluence")

        # Microsoft 365 설정 검증
        if self.SHAREPOINT_SITE_NAME or self.TEAMS_GROUP_NAME:
            if not self.TENANT_ID:
                errors.append("TENANT_ID is required when using Microsoft 365")
            if not self.CLIENT_ID:
                errors.append("CLIENT_ID is required when using Microsoft 365")
            if not self.CLIENT_SECRET:
                errors.append("CLIENT_SECRET is required when using Microsoft 365")

        if errors:
            raise ValueError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))

    def get_auth_header(self, service: str) -> dict:
        """서비스별 인증 헤더 생성"""
        import base64

        if service == "jira":
            auth_str = f"{self.JIRA_EMAIL}:{self.JIRA_API_TOKEN}"
            encoded = base64.b64encode(auth_str.encode()).decode()
            return {
                "Accept": "application/json",
                "Authorization": f"Basic {encoded}"
            }
        elif service == "confluence":
            auth_str = f"{self.CONFLUENCE_EMAIL}:{self.CONFLUENCE_API_TOKEN}"
            encoded = base64.b64encode(auth_str.encode()).decode()
            return {
                "Accept": "application/json",
                "Authorization": f"Basic {encoded}"
            }
        else:
            raise ValueError(f"Unknown service: {service}")


# 싱글톤 인스턴스
settings = Settings()
