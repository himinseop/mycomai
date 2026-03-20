# Docker Compose 실행 가이드

이 문서는 현재 저장소의 [`docker/docker-compose.yml`](/Users/himinseop/Dev/lab/mycomai/docker/docker-compose.yml) 기준으로 정리된 실행 가이드입니다.

## 구성 요약

- `base`: 공통 이미지와 환경 설정
- `data-loader`: 데이터 추출 후 ChromaDB 적재를 수행하는 일회성 작업
- `rag-system`: 대화형 RAG 질의응답 실행
- `cron-scheduler`: 매일 02:00 증분 수집 실행

## 사전 준비

프로젝트 루트에서 `.env`를 준비합니다.

```bash
cp .env.sample .env
```

## 1. 이미지 빌드

프로젝트 루트에서 실행합니다.

```bash
docker-compose -f docker/docker-compose.yml build
```

## 2. 데이터 수집 및 적재

최초 구축 또는 전체 재색인이 필요할 때 사용합니다.

```bash
docker-compose -f docker/docker-compose.yml up data-loader
```

이 서비스는 다음을 순서대로 수행합니다.

1. Jira 추출
2. Confluence 추출
3. SharePoint 추출
4. Teams 추출
5. 모든 JSONL을 합쳐 `data_loader.py`로 적재

출력 파일은 호스트의 `data/` 디렉토리에 남습니다.

## 3. RAG 시스템 실행

```bash
docker-compose -f docker/docker-compose.yml run --rm rag-system
```

- 종료: `exit`
- 표준 입력이 연결된 대화형 세션으로 실행됩니다.

## 4. 크론 기반 증분 수집 실행

```bash
docker-compose -f docker/docker-compose.yml up -d cron-scheduler
```

현재 `docker/crontab` 기준 동작:

- 실행 주기: 매일 02:00
- 기본 증분 범위: `LOOKBACK_DAYS=1`

로그 확인:

```bash
docker-compose -f docker/docker-compose.yml logs -f cron-scheduler
```

## 5. 중지

```bash
docker-compose -f docker/docker-compose.yml down
```

## 참고

- Compose 파일은 `../src/company_llm_rag`를 `/app/company_llm_rag`로 마운트합니다.
- ChromaDB는 `../chroma_db`를 사용합니다.
- 추출 데이터와 에러 로그는 `../data`에 저장됩니다.
