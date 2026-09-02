# Mycomai - RAG AI 프로젝트 컨텍스트

> 이 문서는 코드를 이해하는 데 필요한 정보만 유지합니다.
> 작업 이력·버그 수정 내역은 git log와 GitHub 이슈(docs/issues/)에서 확인하세요.
> `docs/issues/` 하위 디렉토리는 GitHub 이슈 번호와 1:1 대응합니다 (예: `docs/issues/41/` → GitHub Issue #41).

## 프로젝트 개요
회사 전용 LLM RAG 시스템. Jira, Confluence, SharePoint, Teams, 플랫폼매뉴얼(bitbucket docs 저장소) 데이터를 수집하여 ChromaDB에 저장하고, OpenAI GPT를 통해 질의응답하는 시스템.

## 주요 구조
```
src/company_llm_rag/
├── config.py          # 중앙화된 설정 관리 (싱글톤)
├── database.py        # ChromaDB 관리 (Lazy init)
├── logger.py          # 구조화된 로깅 (컬러 출력)
├── data_loader.py     # JSONL → ChromaDB 적재
├── retrieval_module.py # ChromaDB 검색
├── rag_system.py      # RAG 파이프라인 (검색 + LLM)
├── graph/             # GraphRAG (#59) — Jira 구조 그래프 + 집계형 질문 라우팅
│   ├── graph_store.py # nodes/edges (app_data.db) + 조회 템플릿 (자유 SQL 금지)
│   ├── jira_graph.py  # Jira JSONL → 그래프 재구축 (data_loader 훅)
│   ├── query_router.py # intent=aggregate → 전수 조회, 수치·목록은 서버 조립·LLM은 경향 요약만
│   └── entity_link.py # P2: 매뉴얼 백본 엔티티 사전(entities 테이블, 관리자 편집) → 제목 매칭 링크 + 질의 시 주입
├── insight_api/       # 도메인별 LLM 인사이트 API (#56, 내부 솔루션용)
│   ├── router.py      # POST /api/v1/insights/{domain} (인증→검증→통계→LLM→이력)
│   ├── auth.py        # X-API-Key(SHA-256) + scope + IP allowlist
│   ├── ratelimit.py   # 키별 분당 sliding window
│   ├── store.py       # api_clients / api_call_history (app_data.db)
│   └── domains/       # 레지스트리 (sales, voc) — 수치는 서버 선계산, LLM은 해석만
├── platform_docs_store.py  # 플랫폼 문서 S3 수집 상태 (app_data.db, #62)
└── data_extraction/
    ├── jira/          # Jira API v3 (nextPageToken 페이지네이션)
    ├── confluence/    # Confluence REST API (size<limit 페이지네이션)
    ├── m365/
    │   ├── sharepoint_extractor.py
    │   └── teams_extractor.py  # 채널 메시지 + 일반 채팅(TEAMS_CHAT_IDS)
    └── docs_repo/     # 플랫폼매뉴얼 (source=docs)
        ├── docs_extractor.py  # 로컬 git checkout 마크다운 수집 (build_document() — S3 수집과 파싱 로직 공유)
        ├── digest_parser.py   # 다이제스트 카테고리 헤더 파싱 (#61)
        ├── s3_client.py       # S3Client 인터페이스 + Boto3S3Client + FakeS3Client(테스트용) (#62)
        ├── s3_release.py      # current.json/artifact/tar/manifest 검증·안전 해제 (#62)
        └── s3_docs_ingest.py  # S3 수집 오케스트레이터 (#62)
```

## 플랫폼매뉴얼 수집 (source=docs)
bitbucket `o2olab/docs` 저장소의 개발 문서를 지식허브 답변에 활용하는 소스.
- **입력**: 로컬 체크아웃을 data-loader에 read-only 마운트 (`DOCS_REPO_HOST_PATH` → `/app/docs_repo`)
- **브랜치 가드**: `DOCS_REPO_BRANCH` 설정 시 체크아웃 브랜치가 다르면 수집 건너뜀 (git 바이너리 없이 `.git/HEAD` 파싱)
- **검색 우선순위 최상**: `DOCS_RRF_BOOST`(4.0, Hub 5.0 다음·위키 3.0 위) + `BOOST_DOCS`(0.5 거리 부스트)
- **링크 비노출**: 답변 컨텍스트로만 사용. 참고문서 목록 제외, [REF] 인용은 제목만 치환, 프롬프트에 URL 미포함
- README.md는 작성 규칙/목차 문서라 수집 제외

### S3 수집 (source=docs, #62)
Docs CI가 비공개 S3(`o2olab-devops`)에 게시한 아티팩트를 로컬 체크아웃 없이 직접 읽어 색인하는 대안 소스.
`PLATFORM_DOCS_S3_BUCKET` 설정 시 **단일 소스**가 되며 `docs_extractor.py`(로컬 체크아웃)는 자동으로 건너뜀.
- **절차**: `docs/current.json`(sourceCommit·artifact sha256/size) 검증 → 동일 버전이면 스킵 → artifact(`docs/releases/{sourceCommit}/platform.tar.gz`) 다운로드·무결성 검증 → 안전 tar 해제(절대경로·`..`·symlink·hardlink·특수파일·용량 상한 거부) → `catalog.json`/`manifest.json` 버전·해시 검증 → 파싱(문서 파싱 로직은 `docs_extractor.build_document()` 재사용) → id+contentHash로 신규/변경/삭제 판별 → 변경분만 in-process 적재, 삭제분은 Chroma+FTS에서 제거 → 전부 성공한 뒤에만 상태 갱신
- **상태 저장**: `platform_docs_store.py` (app_data.db) — 마지막 성공 sourceCommit + 문서 스냅샷(catalog_id→doc_id·content_hash). 실패 시 이전 성공 상태 그대로 유지
- **실행**: `PYTHONPATH=src python3 company_llm_rag/data_extraction/docs_repo/s3_docs_ingest.py [--force]` (data-loader 파이프라인에 자동 포함, 미설정/실패 시 파이프라인은 계속됨)
- **필요 IAM 권한**: `s3:GetObject`만 (ListBucket 불필요) — `arn:aws:s3:::o2olab-devops/docs/current.json`, `arn:aws:s3:::o2olab-devops/docs/releases/*`
- **환경변수**:
  ```bash
  PLATFORM_DOCS_S3_BUCKET=o2olab-devops       # 비우면 S3 수집 비활성(기본), 로컬 체크아웃 사용
  PLATFORM_DOCS_S3_CURRENT_KEY=docs/current.json
  AWS_REGION=ap-northeast-2
  # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY 등은 .env에 넣지 않음 — 표준 자격증명 체인(IAM 역할) 사용
  PLATFORM_DOCS_MAX_FILES=20000        # tar 해제 상한 (압축 폭탄 방지)
  PLATFORM_DOCS_MAX_FILE_MB=20
  PLATFORM_DOCS_MAX_TOTAL_MB=1024
  ```
- 설계 상세: `docs/issues/62/design.md`

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

# 플랫폼매뉴얼 (bitbucket docs 저장소 수집)
DOCS_REPO_BRANCH=master             # 체크아웃 브랜치 가드 (다르면 수집 건너뜀)
# DOCS_REPO_HOST_PATH=              # 로컬 체크아웃 경로 (기본: mycomai/../../o2olab/docs)
# DOCS_REPO_SUBDIRS=                # 기본: platform/features,platform/sites
# DOCS_RRF_BOOST=4.0  BOOST_DOCS=0.5

# 플랫폼매뉴얼 S3 수집 (#62, 설정 시 위 로컬 체크아웃 수집을 대체)
# PLATFORM_DOCS_S3_BUCKET=o2olab-devops
# PLATFORM_DOCS_S3_CURRENT_KEY=docs/current.json  AWS_REGION=ap-northeast-2

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
