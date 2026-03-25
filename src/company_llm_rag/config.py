"""
중앙화된 설정 관리 모듈

모든 환경변수와 설정을 여기서 관리합니다.
"""

import os
from pathlib import Path
from typing import List, Dict
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()


class Settings:
    """애플리케이션 전역 설정"""

    # OpenAI 설정
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_EMBEDDING_MODEL: str = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    OPENAI_CHAT_MODEL: str = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o")
    OPENAI_SUMMARIZE_MODEL: str = os.getenv("OPENAI_SUMMARIZE_MODEL", "gpt-4o-mini")
    OPENAI_TEMPERATURE: float = float(os.getenv("OPENAI_TEMPERATURE", "0.7"))

    # ChromaDB 설정
    CHROMA_DB_PATH: str = os.getenv("CHROMA_DB_PATH", "./db/chroma_db")
    COLLECTION_NAME: str = os.getenv("COLLECTION_NAME", "company_llm_rag_collection")

    # SQLite DB 경로 (기본값: CHROMA_DB_PATH 상위 디렉토리)
    APP_DATA_DB_PATH: str = os.getenv(
        "APP_DATA_DB_PATH",
        str(Path(os.getenv("CHROMA_DB_PATH", "./db/chroma_db")).parent / "app_data.db"),
    )
    SEARCH_INDEX_DB_PATH: str = os.getenv(
        "SEARCH_INDEX_DB_PATH",
        str(Path(os.getenv("CHROMA_DB_PATH", "./db/chroma_db")).parent / "search_index.db"),
    )

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
    # 환경변수명: CONFLUENCE_SPACE_KEY (콤마 구분 문자열 → 리스트로 변환)
    CONFLUENCE_SPACE_KEYS: List[str] = [
        k.strip() for k in os.getenv("CONFLUENCE_SPACE_KEY", "").split(",") if k.strip()
    ]
    # 환경변수명: CONFLUENCE_SPACE_LABELS (콤마 구분 문자열 → 리스트로 변환)
    CONFLUENCE_SPACE_LABELS: List[str] = [
        k.strip() for k in os.getenv("CONFLUENCE_SPACE_LABELS", "").split(",") if k.strip()
    ]

    # Microsoft 365 설정
    TENANT_ID: str = os.getenv("TENANT_ID", "")
    CLIENT_ID: str = os.getenv("CLIENT_ID", "")
    CLIENT_SECRET: str = os.getenv("CLIENT_SECRET", "")
    SHAREPOINT_SITE_NAME: str = os.getenv("SHAREPOINT_SITE_NAME", "")
    TEAMS_GROUP_NAMES: List[str] = [
        t.strip() for t in os.getenv("TEAMS_GROUP_NAME", "").split(",") if t.strip()
    ]
    TEAMS_CHAT_IDS: List[str] = [
        c.strip() for c in os.getenv("TEAMS_CHAT_IDS", "").split(",") if c.strip()
    ]

    # Teams 문의 채널 설정 (답변 부족 시 메시지 전송) — Incoming Webhook 방식
    # 설정: Teams 채널 → ... → 커넥터 → Incoming Webhook → 구성 → URL 복사
    TEAMS_INQUIRY_WEBHOOK_URL: str = os.getenv("TEAMS_INQUIRY_WEBHOOK_URL", "")


    # RAG 설정 (토큰 기준 — tiktoken 사용)
    # text-embedding-3-small 최대 컨텍스트: 8191 토큰
    # 권장: CHUNK_SIZE 256~512, CHUNK_OVERLAP 32~64
    CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "512"))
    CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "64"))
    RETRIEVAL_TOP_K: int = int(os.getenv("RETRIEVAL_TOP_K", "3"))
    # 청킹에 사용할 tiktoken 인코딩 (임베딩 모델에 맞게 cl100k_base 사용)
    TIKTOKEN_ENCODING: str = os.getenv("TIKTOKEN_ENCODING", "cl100k_base")

    # 페이지네이션 설정
    JIRA_MAX_RESULTS: int = int(os.getenv("JIRA_MAX_RESULTS", "50"))
    CONFLUENCE_PAGE_LIMIT: int = int(os.getenv("CONFLUENCE_PAGE_LIMIT", "25"))

    # 소스별 검색 부스트 가중치 (낮을수록 거리 불이익 — 0~1 사이)
    # .env에서 개별 조정 가능: BOOST_JIRA=0.95
    SOURCE_BOOST_WEIGHTS: Dict[str, float] = {
        "jira":        float(os.getenv("BOOST_JIRA", "0.95")),
        "confluence":  float(os.getenv("BOOST_CONFLUENCE", "0.85")),
        "teams":       float(os.getenv("BOOST_TEAMS", "0.9")),
        "sharepoint":  float(os.getenv("BOOST_SHAREPOINT", "0.7")),
        "local":       float(os.getenv("BOOST_LOCAL", "0.7")),
    }

    # Teams Knowledge Hub 우선순위 부스트
    # TEAMS_KNOWLEDGE_HUB_TEAMS: 쉼표 구분 팀명 (teams_team_name 메타데이터 값, 비워두면 비활성화)
    # BOOST_KNOWLEDGE_HUB_RRF: RRF 점수에 곱할 가중치 (클수록 순위 상승, 기본 3.0)
    TEAMS_KNOWLEDGE_HUB_TEAMS: List[str] = [
        t.strip() for t in os.getenv("TEAMS_KNOWLEDGE_HUB_TEAMS", "").split(",") if t.strip()
    ]
    BOOST_KNOWLEDGE_HUB_RRF: float = float(os.getenv("BOOST_KNOWLEDGE_HUB_RRF", "3.0"))

    # 소스별 검색 필터 키워드 (쿼리에 포함되면 해당 소스만 검색)
    SOURCE_FILTER_KEYWORDS: Dict[str, List[str]] = {
        "jira":        ["지라", "jira", "이슈에서", "이슈로"],
        "confluence":  ["컨플루언스", "컨플에서", "컨플루", "confluence"],
        "teams":       ["팀즈에서", "팀즈 대화", "팀즈에", "teams", "대화에서", "채팅에서", "채널에서"],
        "sharepoint":  ["쉐어포인트", "sharepoint"],
    }

    # 파일 확장자 → 소스 매핑 (쿼리에 해당 키워드가 있으면 extensions 필터 적용)
    EXTENSION_FILTER_KEYWORDS: Dict[str, List[str]] = {
        ".xlsx,.xls":   ["엑셀", "excel", ".xlsx", ".xls"],
        ".pptx,.ppt":   ["ppt", "파워포인트", "기획서", "발표자료", "프레젠테이션"],
        ".docx,.doc":   [".docx", ".doc", "word 문서"],
        ".pdf":         [".pdf", " pdf "],
    }

    # 페르소나 설정
    AI_NAME: str = os.getenv("AI_NAME", "")
    COMPANY_NAME: str = os.getenv("COMPANY_NAME", "")
    COMPANY_DESCRIPTION: str = os.getenv("COMPANY_DESCRIPTION", "")

    # 프롬프트 파일 경로 (비워두면 패키지 내 기본 파일 사용)
    # system_prompt.txt: LLM system 메시지 (페르소나/보안 규칙)
    # rag_instructions.txt: RAG 답변 규칙 (출처 형식, 슬라이드 번호 등)
    SYSTEM_PROMPT_FILE: str = os.getenv("SYSTEM_PROMPT_FILE", "")
    RAG_INSTRUCTIONS_FILE: str = os.getenv("RAG_INSTRUCTIONS_FILE", "")

    # 어드민 대시보드 접근 비밀번호 (비워두면 /admin 비활성화)
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")

    # 로깅 설정
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # SQLite 저널 모드
    # DELETE: 본파일에 직접 반영되어 호스트에서 db 파일만 읽어도 최신 상태 확인 가능
    # WAL: 동시성/성능에 유리하지만 최신 내용이 -wal/-shm에 남을 수 있음
    SQLITE_JOURNAL_MODE: str = os.getenv("SQLITE_JOURNAL_MODE", "DELETE").upper()

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
