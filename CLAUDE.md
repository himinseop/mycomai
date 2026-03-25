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
# 데이터 수집 + 임베딩 + ChromaDB 적재 (한 번에)
docker-compose -f docker/docker-compose.yml up data-loader

# RAG 질의응답 실행
docker-compose -f docker/docker-compose.yml run --rm rag-system

# DB 문서 수 확인
PYTHONPATH=src python3 -c "from company_llm_rag.database import db_manager; print(db_manager.get_collection_stats())"

# 설정 확인 (로컬)
PYTHONPATH=src python3 -c "from company_llm_rag.config import settings; print(settings.COLLECTION_NAME)"
```

## chat_history 스키마 (현재)
| 컬럼 | 타입 | 설명 |
|---|---|---|
| `session_id` | TEXT | 질문 그룹 ID (새 질문마다 신규 생성) |
| `turn_index` | INTEGER | 그룹 내 몇 번째 턴 (1부터 시작) |
| `parent_record_id` | INTEGER | 직전 턴의 `id` (첫 턴은 NULL) |
| `feedback` | INTEGER | 단건 턴 피드백 (1 / -1 / 0) |
| `group_feedback` | INTEGER | 질문 그룹 대표 피드백 (1 / -1 / 0) |
| `group_feedback_at` | TEXT | 그룹 피드백 입력 시각 (ISO8601) |

## 참고 문서
- `REFACTORING_PLAN.md` - 전체 리팩토링 로드맵 및 진행 상황
- `DEDUPLICATION_STRATEGIES.md` - 중복 수집 최소화 전략
- `docker/docker_compose_instructions.md` - Docker 실행 가이드
- `docs/issues/37_question_group_session_redesign_2026-03-25.md` - Issue #37 설계 문서 및 TC
