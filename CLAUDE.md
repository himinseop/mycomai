# Mycomai - RAG AI 프로젝트 컨텍스트

> 이 문서는 코드를 이해하는 데 필요한 정보만 유지합니다.
> 작업 이력·버그 수정 내역은 git log와 GitHub 이슈(docs/issues/)에서 확인하세요.
> `docs/issues/` 하위 디렉토리는 GitHub 이슈 번호와 1:1 대응합니다 (예: `docs/issues/41/` → GitHub Issue #41).

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

# Knowledge Hub (답변 우선순위 + 질문/피드백 전송)
KNOWLEDGE_HUB_TEAM_NAME=Knowledge Hub
KNOWLEDGE_HUB_WEBHOOK_URL=...       # Incoming Webhook URL
KNOWLEDGE_HUB_RRF_BOOST=5.0         # RRF 점수 배수

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

## Knowledge Hub 직접 응답 아키텍처
Knowledge Hub 팀 채널에 담당자가 작성한 Q&A 답변을, 유사 질문 인입 시 원문 그대로 제공하는 시스템.

### 데이터 흐름
```
[수집] Teams Knowledge Hub 채널
  → Adaptive Card에서 [질문] 텍스트 추출
  → Reply HTML에서 답변 원문 + 이미지(Graph API→로컬 다운로드) 추출
  → ChromaDB: 질문만 임베딩 (is_hub_direct=True 메타데이터)
  → SQLite hub_replies: 답변 원문 저장 (이미지 마크다운 인라인)

[질의] 사용자 질문 인입
  → 벡터 검색 (RRF 5.0x 부스트로 Hub 문서 우선)
  → 1위가 Hub 문서 & 2위 대비 2배 이상 우세?
    → Yes: gpt-4o-mini로 안내 멘트 생성 + SQLite에서 답변 원문 직접 반환
    → No:  기존 LLM RAG 파이프라인
```

### 주요 동작
- **임베딩**: 질문 텍스트만 ChromaDB에 저장 (Adaptive Card에서 `[질문]` 추출)
- **원문 저장**: `hub_replies` 테이블(app_data.db)에 reply 원문 + 이미지 마크다운 보관
- **검색**: 질문 임베딩으로 유사 질문 매칭, RRF 5.0x 부스트
- **응답**: 안내 멘트(gpt-4o-mini) + 답변 원문(수정 없이 그대로, 이미지 포함)
- **중복 처리**: 동일 질문 감지 시 기존 임베딩 재활용, 답변 포인터만 변경 (이전 답변은 is_active=0으로 이력 보관)
- **참고문서**: Hub 직접 응답 시 비표시

### hub_replies 스키마 (app_data.db)
| 컬럼 | 타입 | 설명 |
|---|---|---|
| `doc_id` | TEXT | 원본 Teams 메시지 ID |
| `question` | TEXT | 질문 텍스트 |
| `reply_content` | TEXT | 답변 원문 (마크다운 이미지 포함) |
| `created_at` | TEXT | 답변 저장 시각 (ISO8601) |
| `is_active` | INTEGER | 활성 답변 여부 (1=현재, 0=이전 버전) |

### 관련 파일
| 파일 | 역할 |
|------|------|
| `config.py` | `KNOWLEDGE_HUB_TEAM_NAME`, `KNOWLEDGE_HUB_WEBHOOK_URL`, `KNOWLEDGE_HUB_RRF_BOOST` |
| `teams_extractor.py` | Adaptive Card 질문 추출, reply 이미지 다운로드, 질문/답변 분리 |
| `data_loader.py` | 질문만 임베딩, 답변 원문 SQLite 저장, 중복 질문 감지 |
| `history_store.py` | `hub_upsert`, `hub_get_reply`, `hub_find_duplicate`, `hub_get_reply_history` |
| `rag_system.py` | `_try_hub_direct_answer`, `_build_hub_intro` (안내 멘트 LLM 생성) |
| `retrieval_module.py` | RRF 부스트 적용 |
| `static/images/` | 다운로드된 Teams 이미지 파일 |

## 참고 문서
- `REFACTORING_PLAN.md` - 전체 리팩토링 로드맵 및 진행 상황
- `DEDUPLICATION_STRATEGIES.md` - 중복 수집 최소화 전략
- `docker/docker_compose_instructions.md` - Docker 실행 가이드
- `docs/issues/37/design.md` - Issue #37 설계 문서 및 TC
