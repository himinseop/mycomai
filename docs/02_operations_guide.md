# 02. 운영 가이드

## 사전 요구사항

- Docker Engine 또는 Docker Desktop
- Docker Compose v2 (`docker compose`)
- OpenAI API 키
- 필요한 외부 시스템 접근 권한

## 초기 준비

```bash
cp .env.sample .env
mkdir -p db/chroma_db data
touch db/query_history.db
```

첫 실행 전에는 `.env` 에 실제 자격 증명을 채워야 합니다.

## 주요 디렉토리

```text
mycomai/
├── data/                     # 수집된 JSONL 및 추출 로그
├── db/                       # 런타임 데이터
│   ├── chroma_db/            # ChromaDB 영속 저장소
│   └── query_history.db      # SQLite (이력, FTS5, 앱 설정)
├── docker/                   # Dockerfile, compose, cron 설정
├── docs/                     # 프로젝트 문서
├── src/company_llm_rag/      # 애플리케이션 코드
├── tests/                    # 테스트
├── .env                      # 실제 환경 변수
└── .env.sample               # 환경 변수 템플릿
```

## 자주 쓰는 명령어

### 이미지 빌드

```bash
docker compose -f docker/docker-compose.yml build
```

### 웹 서버 실행

```bash
docker compose -f docker/docker-compose.yml up -d web
```

접속 주소: `http://localhost:8000`

### 웹 서버 중지

```bash
docker compose -f docker/docker-compose.yml stop web
```

### 전체 수집 및 적재

```bash
docker compose -f docker/docker-compose.yml up data-loader
```

이 작업은 다음 순서로 진행됩니다.

1. Jira 추출
2. Confluence 추출
3. SharePoint 추출
4. Teams 추출
5. JSONL 병합 후 ChromaDB/FTS 적재

### 대화형 RAG 실행

```bash
docker compose -f docker/docker-compose.yml run --rm rag-system
```

### FTS5 인덱스 재구축

```bash
docker compose -f docker/docker-compose.yml run --rm web python -m company_llm_rag.rebuild_fts
```

### 자동 증분 수집 스케줄러 실행

```bash
docker compose -f docker/docker-compose.yml up -d cron-scheduler
```

기본 스케줄은 매일 새벽 2시이며, `docker/crontab` 에서 관리합니다.

## 로컬 실행 예시

컨테이너 없이 실행할 때는 `PYTHONPATH=src` 를 명시합니다.

```bash
PYTHONPATH=src python3 -m company_llm_rag.rebuild_fts
PYTHONPATH=src uvicorn company_llm_rag.web_app:app --host 0.0.0.0 --port 8000 --reload
```

## 주요 환경 변수

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `OPENAI_API_KEY` | OpenAI API 키 | 없음 |
| `OPENAI_CHAT_MODEL` | 답변 생성 모델 | `gpt-4o` |
| `OPENAI_SUMMARIZE_MODEL` | Teams 문의 요약 모델 | `gpt-4o-mini` |
| `OPENAI_EMBEDDING_MODEL` | 임베딩 모델 | `text-embedding-3-small` |
| `CHROMA_DB_PATH` | ChromaDB 저장 경로 | `./db/chroma_db` |
| `COLLECTION_NAME` | ChromaDB 컬렉션명 | `company_llm_rag_collection` |
| `CHUNK_SIZE` | 청크 크기(토큰) | `512` |
| `CHUNK_OVERLAP` | 청크 오버랩(토큰) | `64` |
| `RETRIEVAL_TOP_K` | 기본 검색 결과 개수 | `3` |
| `LOOKBACK_DAYS` | 증분 수집 기간(일) | 비어 있으면 전체 |
| `ADMIN_PASSWORD` | `/admin` 접근 비밀번호 | 비어 있으면 비활성화 |
| `TEAMS_INQUIRY_WEBHOOK_URL` | Teams 문의 Webhook | 없음 |

소스별 상세 변수는 `.env.sample` 을 기준으로 관리하는 것이 가장 안전합니다.

## 운영 점검

### 컨테이너 로그 확인

```bash
docker compose -f docker/docker-compose.yml logs web --tail=100
docker compose -f docker/docker-compose.yml logs cron-scheduler --tail=100
docker compose -f docker/docker-compose.yml logs data-loader --tail=100
```

### ChromaDB 문서 수 확인

```bash
docker compose -f docker/docker-compose.yml run --rm web python -c \
  "from company_llm_rag.database import db_manager; print(db_manager.get_collection_stats())"
```

### SQLite 무결성 확인

```bash
sqlite3 db/query_history.db "PRAGMA integrity_check;"
```

정상이라면 `ok` 가 출력됩니다.

### 질의 이력 DB를 본파일로 직접 확인하고 싶을 때

- 기본 설정은 `SQLITE_JOURNAL_MODE=DELETE` 입니다.
- 이 모드에서는 최신 커밋이 `db/query_history.db` 본파일에 바로 반영됩니다.
- 따라서 `sqlite3 db/query_history.db` 로 직접 조회해도 최신 이력을 볼 수 있습니다.
- 반대로 `WAL` 모드에서는 최신 변경이 `query_history.db-wal` 에 남을 수 있어 본파일만 보면 일부 이력이 빠질 수 있습니다.

### FTS 문서 수 확인

```bash
sqlite3 db/query_history.db "SELECT COUNT(*) FROM doc_fts;"
```

### 질의 이력 단건 조회

도커 컨테이너에 들어가지 않아도 호스트에 바인드된 DB를 바로 읽을 수 있습니다.

```bash
python3 scripts/query_history.py 83
python3 scripts/query_history.py 83 --analysis
python3 scripts/query_history.py --tail 10
```

이 스크립트는 `db/query_history.db` 와 함께 `db/query_history.db-wal`, `db/query_history.db-shm` 존재 여부와 크기도 같이 보여줍니다.

## 백업

### ChromaDB 백업

```bash
cp -R db/chroma_db db/chroma_db.bak
```

### 이력 DB 백업

```bash
cp db/query_history.db db/query_history.db.bak
```

## 장애 대응

### FTS 검색이 비거나 이상할 때

```bash
docker compose -f docker/docker-compose.yml run --rm web python -m company_llm_rag.rebuild_fts
```

### 웹 응답이 멈췄을 때

```bash
docker compose -f docker/docker-compose.yml restart web
```

### 증분 수집 범위를 줄이고 싶을 때

`.env` 에 다음처럼 설정합니다.

```env
LOOKBACK_DAYS=1
```

### 답변 부족 시 사람 문의를 활성화하고 싶을 때

`.env` 에 `TEAMS_INQUIRY_WEBHOOK_URL` 을 설정하면 됩니다. 답변을 찾지 못한 경우 웹 UI에서 Teams 문의 버튼을 노출합니다.
