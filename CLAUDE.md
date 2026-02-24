# Mycomai - RAG AI 프로젝트 컨텍스트

## 프로젝트 개요
회사 전용 LLM RAG 시스템. Jira, Confluence, SharePoint, Teams 데이터를 수집하여 ChromaDB에 저장하고, OpenAI GPT를 통해 질의응답하는 시스템.

## 주요 구조
```
src/company_llm_rag/
├── config.py          # 중앙화된 설정 관리 (싱글톤)
├── database.py        # ChromaDB 관리 (Lazy init)
├── logger.py          # 구조화된 로깅 (컬러 출력)
├── data_loader.py     # JSONL → ChromaDB 적재
├── retrieval_module.py # ChromaDB 검색
├── rag_system.py      # RAG 파이프라인 (검색 + LLM)
└── data_extraction/
    ├── jira/          # Jira API v3 (nextPageToken 페이지네이션)
    ├── confluence/    # Confluence REST API (size<limit 페이지네이션)
    └── m365/
        ├── sharepoint_extractor.py
        └── teams_extractor.py  # 채널 메시지 + 일반 채팅(TEAMS_CHAT_IDS)
```

## 실행 환경
- Docker Compose 기반 (`docker/docker-compose.yml`)
- Python 3.11 (Docker), 3.9 (로컬)
- PYTHONPATH=/app (Docker 내부)
- 로컬 실행: `PYTHONPATH=src python3 ...`

## 현재 상태 (2026-02-24 기준)

### 완료된 작업 (Phase 1 + Quick Win)
- ✅ 패키지 구조 개선 (`__init__.py`, 절대 import)
- ✅ 중앙화된 설정 관리 (`config.py`)
- ✅ 구조화된 로깅 (`logger.py`, 컬러 출력)
- ✅ 의존성 버전 고정 (`requirements.txt`)
- ✅ Jira 페이지네이션 버그 수정 (v3 API nextPageToken 방식으로 변경)
- ✅ Confluence 페이지네이션 버그 수정 (`size < limit` 체크)
- ✅ SharePoint/Teams extractor 리팩토링
- ✅ Teams 일반 채팅 수집 기능 추가 (`TEAMS_CHAT_IDS`)

### 발견된 버그 및 수정 내역
- **Jira**: `/rest/api/3/search/jql`은 `total` 필드 없음 → `isLast` + `nextPageToken` 사용
- **Confluence**: `total` 필드 없음 → `size < limit`으로 마지막 페이지 판단
- **Teams 채팅**: `/v1.0/chats` 엔드포인트는 Application context 미지원 → `/chats/{id}/messages` 직접 호출

### 수집된 데이터 현황
- 기존 ChromaDB: **48,485개** 문서 (페이지네이션 버그 있던 상태로 수집)
- 예상 재수집 후: Jira/Confluence 대폭 증가 예정
- Teams 채팅 3개 추가 예정 (업무소통방, 슈퍼커넥트 전사방, 대표님과 함께하는 조직장들)

### 다음 할 일
- [ ] 데이터 재수집 (페이지네이션 버그 수정 반영)
- [ ] Teams 채팅 데이터 수집 및 ChromaDB 적재
- [ ] Phase 2 작업: 재시도 로직, 타입 힌트, 테스트 커버리지

## 필수 환경변수 (.env)
```bash
# Jira
JIRA_BASE_URL=https://o2olab.atlassian.net
JIRA_API_TOKEN=...
JIRA_EMAIL=...
JIRA_PROJECT_KEY=CUPPING,WMPO,WPLUS

# Confluence
CONFLUENCE_BASE_URL=https://o2olab.atlassian.net/wiki
CONFLUENCE_API_TOKEN=...
CONFLUENCE_EMAIL=...
CONFLUENCE_SPACE_KEY=O2

# Microsoft 365
TENANT_ID=...
CLIENT_ID=...
CLIENT_SECRET=...
SHAREPOINT_SITE_NAME=o2olab group
TEAMS_GROUP_NAME=...
TEAMS_CHAT_IDS=19:40aa52f10c82483382591a326c49c01a@thread.v2,19:692046332e64487c9108419d5341720a@thread.v2,19:d1224a505a37480b992c796a42a322ae@thread.v2

# OpenAI
OPENAI_API_KEY=...

# 선택사항
LOG_LEVEL=INFO
LOOKBACK_DAYS=  # 비워두면 전체 수집
```

## Azure AD 앱 권한 목록
현재 설정된 Application 권한:
- `Group.Read.All`
- `Sites.Read.All`
- `ChannelMessage.Read.All`
- `Application.Read.All`
- `Chat.Read.All` (일반 채팅 수집용, 추가됨)
- `User.Read.All` (채팅방 목록 조회용, 추가됨)

## 주요 명령어
```bash
# 로컬 테스트
PYTHONPATH=src python3 -c "from company_llm_rag.config import settings; print(settings.COLLECTION_NAME)"

# 데이터 수집 (개별)
PYTHONPATH=src python3 src/company_llm_rag/data_extraction/jira/jira_extractor.py > data/jira_data.jsonl
PYTHONPATH=src python3 src/company_llm_rag/data_extraction/confluence/confluence_extractor.py > data/confluence_data.jsonl
PYTHONPATH=src python3 src/company_llm_rag/data_extraction/m365/teams_extractor.py > data/teams_data.jsonl

# ChromaDB 적재
PYTHONPATH=src python3 src/company_llm_rag/data_loader.py < data/jira_data.jsonl

# Docker
docker-compose -f docker/docker-compose.yml up data-loader
docker-compose -f docker/docker-compose.yml run --rm rag-system

# DB 상태 확인
PYTHONPATH=src python3 -c "from company_llm_rag.database import db_manager; print(db_manager.get_collection_stats())"
```

## 참고 문서
- `REFACTORING_PLAN.md` - 전체 리팩토링 로드맵 및 진행 상황
- `DEDUPLICATION_STRATEGIES.md` - 중복 수집 최소화 전략
- `docker/docker_compose_instructions.md` - Docker 실행 가이드
