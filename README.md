# Mycomai

회사 내부 협업 도구의 데이터를 수집해 ChromaDB에 적재하고, OpenAI 기반 질의응답을 제공하는 사내용 RAG 시스템입니다. 현재 수집 대상은 Jira, Confluence, SharePoint, Teams 채널 메시지, Teams 일반 채팅입니다.

## 현재 기준

- 문서 기준일: 2026-03-19
- 권장 실행 방식: Docker Compose
- 로컬 개발 실행: `PYTHONPATH=src python3 ...`
- Python 버전:
  - Docker: 3.11
  - 로컬: 3.9 이상 권장

## 주요 기능

- Jira, Confluence, SharePoint, Teams 데이터 수집
- Teams 일반 채팅 수집 (`TEAMS_CHAT_IDS`)
- ChromaDB 기반 벡터 저장소
- OpenAI 임베딩 + Chat Completions 기반 RAG 질의응답
- `content_hash` 기반 변경 없는 청크 임베딩 스킵
- `LOOKBACK_DAYS` 기반 증분 수집

## 프로젝트 구조

```text
src/company_llm_rag/
├── config.py
├── database.py
├── data_loader.py
├── logger.py
├── rag_system.py
├── retrieval_module.py
└── data_extraction/
    ├── jira/
    ├── confluence/
    └── m365/
```

기타 주요 경로:

- `docker/docker-compose.yml`: 운영용 Compose 정의
- `docker/crontab`: 일일 증분 수집 스케줄
- `tests/`: 기본 유닛 테스트
- `data/`: 추출 결과와 에러 로그
- `chroma_db/`: 로컬 ChromaDB 저장소

## 빠른 시작

### 1. 저장소 준비

```bash
git clone https://github.com/himinseop/mycomai.git
cd mycomai
cp .env.sample .env
```

`.env`를 실제 값으로 수정합니다.

### 2. 필수 환경변수

```env
# OpenAI
OPENAI_API_KEY=

# Jira
JIRA_BASE_URL=
JIRA_EMAIL=
JIRA_API_TOKEN=
JIRA_PROJECT_KEY=

# Confluence
CONFLUENCE_BASE_URL=
CONFLUENCE_EMAIL=
CONFLUENCE_API_TOKEN=
CONFLUENCE_SPACE_KEY=

# Microsoft 365
TENANT_ID=
CLIENT_ID=
CLIENT_SECRET=
SHAREPOINT_SITE_NAME=
TEAMS_GROUP_NAME=
TEAMS_CHAT_IDS=
```

선택 환경변수:

```env
LOOKBACK_DAYS=
LOG_LEVEL=INFO
CHUNK_SIZE=512
CHUNK_OVERLAP=64
RETRIEVAL_TOP_K=3
OPENAI_CHAT_MODEL=gpt-4o
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
JIRA_MAX_RESULTS=50
CONFLUENCE_PAGE_LIMIT=25
```

## Docker Compose 실행

### 이미지 빌드

```bash
docker-compose -f docker/docker-compose.yml build
```

### 전체 수집 + 적재

초기 구축이나 페이지네이션 수정 반영 시 전체 수집을 권장합니다.

```bash
docker-compose -f docker/docker-compose.yml up data-loader
```

이 작업은 아래 순서로 실행됩니다.

1. Jira 추출
2. Confluence 추출
3. SharePoint 추출
4. Teams 추출
5. 추출된 JSONL을 `data_loader.py`에 파이프해 ChromaDB 적재

생성 파일:

- `data/jira_data.jsonl`
- `data/confluence_data.jsonl`
- `data/sharepoint_data.jsonl`
- `data/teams_data.jsonl`
- `data/*_errors.log`

### RAG 질의응답 실행

```bash
docker-compose -f docker/docker-compose.yml run --rm rag-system
```

종료는 `exit`를 입력하면 됩니다.

### 증분 수집 스케줄러 실행

크론 컨테이너는 매일 02:00에 실행되며, 내부적으로 `LOOKBACK_DAYS=1`을 사용합니다.

```bash
docker-compose -f docker/docker-compose.yml up -d cron-scheduler
```

로그 확인:

```bash
docker-compose -f docker/docker-compose.yml logs -f cron-scheduler
```

## 로컬 실행

로컬에서는 `PYTHONPATH=src`가 필요합니다.

### 설정 확인

```bash
PYTHONPATH=src python3 -c "from company_llm_rag.config import settings; print(settings.COLLECTION_NAME)"
```

### DB 상태 확인

```bash
PYTHONPATH=src python3 -c "from company_llm_rag.database import db_manager; print(db_manager.get_collection_stats())"
```

### 개별 추출기 실행 예시

```bash
PYTHONPATH=src python3 src/company_llm_rag/data_extraction/jira/jira_extractor.py > data/jira_data.jsonl
PYTHONPATH=src python3 src/company_llm_rag/data_extraction/confluence/confluence_extractor.py > data/confluence_data.jsonl
```

### 로컬에서 RAG 실행

```bash
PYTHONPATH=src python3 src/company_llm_rag/rag_system.py
```

## 증분 수집과 중복 방지

- `LOOKBACK_DAYS`가 설정되면 각 extractor가 최근 변경분만 조회합니다.
- `data_loader.py`는 각 청크의 MD5 해시를 `content_hash`로 저장합니다.
- 같은 `chunk_id`의 `content_hash`가 같으면 `upsert()`를 건너뛰어 임베딩 비용을 줄입니다.
- 실행 결과는 `new / updated / skipped / failed` 형태로 로그에 출력됩니다.

## 운영 메모

- Jira는 `/rest/api/3/search/jql`의 `nextPageToken` 페이지네이션을 사용합니다.
- Confluence는 `size < limit` 기준으로 마지막 페이지를 판별합니다.
- Teams 일반 채팅은 `/chats/{id}/messages`를 직접 호출합니다.
- Teams 일반 채팅 수집에는 `Chat.Read.All`, `User.Read.All` Application 권한이 필요합니다.

## 테스트

테스트 파일은 `tests/`에 있습니다.

```bash
python3 -m pip install -r src/requirements-dev.txt
PYTHONPATH=src python3 -m pytest -q
```

현재 저장소 상태에서는 로컬 환경에 `pytest`가 설치되어 있지 않으면 테스트가 실행되지 않습니다.

## 관련 문서

- [REFACTORING_PLAN.md](/Users/himinseop/Dev/lab/mycomai/REFACTORING_PLAN.md)
- [DEDUPLICATION_STRATEGIES.md](/Users/himinseop/Dev/lab/mycomai/DEDUPLICATION_STRATEGIES.md)
- [PAGINATION_FIX.md](/Users/himinseop/Dev/lab/mycomai/PAGINATION_FIX.md)
- [docker/docker_compose_instructions.md](/Users/himinseop/Dev/lab/mycomai/docker/docker_compose_instructions.md)
- [docker/docker_instructions.md](/Users/himinseop/Dev/lab/mycomai/docker/docker_instructions.md)
