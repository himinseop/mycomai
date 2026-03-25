# Mycomai

사내 협업 도구의 데이터를 수집하고, ChromaDB 기반 검색과 OpenAI 응답 생성을 결합해 내부 지식 검색을 제공하는 회사 전용 RAG 시스템입니다.

현재 코드는 다음 기능을 중심으로 구성되어 있습니다.

- Jira, Confluence, SharePoint, Teams 데이터 수집
- JSONL 기반 적재 파이프라인과 ChromaDB 벡터 저장
- SQLite FTS5를 이용한 하이브리드 검색
- FastAPI 웹 채팅 UI와 어드민 대시보드
- 답변 부족 시 Teams 문의 전송 및 불만족 응답 분석

문서는 모두 [`docs`](./docs) 아래로 정리했습니다.

- [`docs/README.md`](./docs/README.md): 문서 목차
- [`docs/01_system_overview.md`](./docs/01_system_overview.md): 아키텍처와 핵심 동작
- [`docs/02_operations_guide.md`](./docs/02_operations_guide.md): 실행, 운영, 점검 절차
- [`docs/03_project_structure.md`](./docs/03_project_structure.md): 디렉토리와 모듈 분석
- [`docs/04_docker_deployment_guide.md`](./docs/04_docker_deployment_guide.md): Docker/Docker Compose 사용법

빠르게 시작하려면:

```bash
cp .env.sample .env
mkdir -p db/chroma_db data
touch db/query_history.db
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml up -d web
```

웹 UI는 `http://localhost:8000` 에서 확인할 수 있습니다.

질의 이력을 로컬에서 바로 확인하려면:

```bash
python3 scripts/query_history.py 83
python3 scripts/query_history.py --tail 10
```

`query_history.db` 본파일만 직접 확인하고 싶다면 SQLite 저널 모드를 `DELETE`로 유지해야 합니다.
현재 Docker Compose 기본값도 `DELETE`로 맞춰 두었습니다.
