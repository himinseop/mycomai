# Docker 단독 실행 가이드

일반 운영은 Docker Compose 사용을 권장합니다. 이 문서는 디버깅이나 수동 점검을 위해 단일 컨테이너로 실행할 때 참고하는 가이드입니다.

기준 파일:

- [`docker/Dockerfile`](/Users/himinseop/Dev/lab/mycomai/docker/Dockerfile)

## 1. 이미지 빌드

프로젝트 루트에서 실행합니다.

```bash
docker build -f docker/Dockerfile -t mycomai-rag .
```

## 2. 대화형 컨테이너 실행

```bash
docker run -it --rm \
  --env-file .env \
  -v "$(pwd)/chroma_db:/app/chroma_db" \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/src/company_llm_rag:/app/company_llm_rag" \
  mycomai-rag \
  bash
```

설명:

- `--env-file .env`: 환경변수 주입
- `-v "$(pwd)/chroma_db:/app/chroma_db"`: ChromaDB 영속화
- `-v "$(pwd)/data:/app/data"`: 추출 결과와 로그 보존
- `-v "$(pwd)/src/company_llm_rag:/app/company_llm_rag"`: 코드 변경 즉시 반영

## 3. 컨테이너 내부에서 실행할 수 있는 명령

### 설정 확인

```bash
python3 -c "from company_llm_rag.config import settings; print(settings.COLLECTION_NAME)"
```

### Jira 추출

```bash
python3 company_llm_rag/data_extraction/jira/jira_extractor.py > data/jira_data.jsonl
```

### Confluence 추출

```bash
python3 company_llm_rag/data_extraction/confluence/confluence_extractor.py > data/confluence_data.jsonl
```

### SharePoint 추출

```bash
python3 company_llm_rag/data_extraction/m365/sharepoint_extractor.py > data/sharepoint_data.jsonl
```

### Teams 추출

```bash
python3 company_llm_rag/data_extraction/m365/teams_extractor.py > data/teams_data.jsonl
```

### 적재

```bash
cat data/jira_data.jsonl data/confluence_data.jsonl data/sharepoint_data.jsonl data/teams_data.jsonl | python3 company_llm_rag/data_loader.py
```

### RAG 실행

```bash
python3 company_llm_rag/rag_system.py
```

## 4. 종료

컨테이너 내부에서 `exit`를 입력하면 종료됩니다.
