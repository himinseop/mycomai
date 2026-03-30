# 04. 도커 배포 가이드

기존에 `docker/` 아래 흩어져 있던 Docker 설명을 현재 구성 기준으로 통합한 문서입니다.

## 구성 파일

| 파일 | 역할 |
|------|------|
| `docker/Dockerfile` | Python 3.11 기반 애플리케이션 이미지 빌드 |
| `docker/docker-compose.yml` | 실행 서비스 정의 |
| `docker/crontab` | 자동 증분 수집 스케줄 |
| `docker/cron-entrypoint.sh` | cron 컨테이너 시작 스크립트 |

## Docker 이미지

이미지는 루트 컨텍스트를 사용하고, `docker/Dockerfile` 로 빌드합니다.

주요 특징:

- 베이스 이미지: `python:3.11-slim-bookworm`
- 필수 패키지 설치: `cron`
- Python 의존성 설치: `src/requirements.txt`
- 애플리케이션 코드 복사: `src/company_llm_rag/`
- 기본 환경 변수:
  - `PYTHONPATH=/app`
  - `CHROMA_DB_PATH=./db/chroma_db`
  - `COLLECTION_NAME=company_llm_rag_collection`
  - `SQLITE_JOURNAL_MODE=DELETE`

## Compose 서비스

### `base`

공통 설정을 담는 부모 서비스입니다.

- 이미지 빌드
- `.env` 로드
- 볼륨 마운트
  - `../db:/app/db`
  - `../src/company_llm_rag:/app/company_llm_rag`

### `data-loader`

일회성 배치 서비스입니다.

- 각 추출기를 순차 실행합니다.
- 결과를 `data/*.jsonl` 로 저장합니다.
- 모든 JSONL을 `data_loader.py` 로 흘려 ChromaDB/FTS에 적재합니다.
- Knowledge Hub 문서는 질문만 임베딩하고, 답변 원문은 `app_data.db`의 `hub_replies` 테이블에 저장합니다.
- Knowledge Hub 이미지는 Graph API에서 다운로드하여 `static/images/`에 캐시합니다.

실행:

```bash
docker compose -f docker/docker-compose.yml up data-loader
```

### `rag-system`

CLI 형태의 대화형 실행 서비스입니다.

실행:

```bash
docker compose -f docker/docker-compose.yml run --rm rag-system
```

### `web`

실제 웹 서버 서비스입니다.

- `uvicorn company_llm_rag.web_app:app`
- 포트 `8000:8000`
- `restart: unless-stopped`

실행:

```bash
docker compose -f docker/docker-compose.yml up -d web
```

### `cron-scheduler`

자동 수집 전용 서비스입니다.

- `docker/crontab` 을 `/etc/cron.d/my-cron` 으로 마운트
- `docker/cron-entrypoint.sh` 로 cron 실행
- `data/` 디렉토리 마운트

실행:

```bash
docker compose -f docker/docker-compose.yml up -d cron-scheduler
```

## 배포 순서

### 1. 환경 준비

```bash
cp .env.sample .env
mkdir -p db/chroma_db data
```

### 2. 이미지 빌드

```bash
docker compose -f docker/docker-compose.yml build
```

### 3. 최초 데이터 적재

```bash
docker compose -f docker/docker-compose.yml up data-loader
```

### 4. 웹 서비스 기동

```bash
docker compose -f docker/docker-compose.yml up -d web
```

### 5. 필요 시 자동 수집 기동

```bash
docker compose -f docker/docker-compose.yml up -d cron-scheduler
```

## 스케줄러 동작 방식

현재 `docker/crontab` 기준 설정은 다음과 같습니다.

- `LOOKBACK_DAYS=1`
- 매일 02:00에 추출 + 적재 실행

즉, 기본 운영 모델은 다음과 같습니다.

- 최초 1회 전체 적재
- 이후 cron 컨테이너로 최근 1일 변경분 증분 반영

## 주의할 점

- Compose 파일은 `docker compose` 와 함께 `docker/docker-compose.yml` 을 직접 지정하는 방식이 가장 명확합니다.
- 현재 기본 모드는 `DELETE` 이므로 최신 질의 이력은 `db/app_data.db` 본파일에서 바로 확인할 수 있습니다.
- 만약 `WAL` 로 바꾸면 읽기/쓰기 동시성은 좋아지지만 최신 변경이 `app_data.db-wal`, `app_data.db-shm` 로 분리될 수 있습니다.
- `web` 서비스만으로도 대부분의 사용자 기능을 사용할 수 있습니다.
- `rag-system` 은 웹 UI 대체 수단이지 주 서비스는 아닙니다.
