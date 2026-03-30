# 03. 프로젝트 구조

## 한눈에 보는 구조

```text
.
├── data/
├── db/
├── docker/
├── docs/
├── src/
│   └── company_llm_rag/
│       ├── data_extraction/
│       ├── llm/
│       ├── prompts/
│       ├── static/
│       ├── templates/
│       ├── config.py
│       ├── data_loader.py
│       ├── database.py
│       ├── history_store.py
│       ├── no_answer_analyzer.py
│       ├── rag_system.py
│       ├── retrieval_module.py
│       ├── teams_sender.py
│       └── web_app.py
└── tests/
```

## 디렉토리별 분석

### `src/company_llm_rag/`

프로젝트의 핵심 애플리케이션 코드입니다.

- `config.py`: 환경 변수 로드와 설정값 정규화
- `database.py`: ChromaDB 클라이언트/컬렉션 접근
- `data_loader.py`: 적재 파이프라인의 중심
- `retrieval_module.py`: 하이브리드 검색 로직
- `rag_system.py`: 검색 결과를 바탕으로 프롬프트 생성 및 LLM 호출
- `web_app.py`: FastAPI 엔드포인트와 웹 서비스 진입점
- `history_store.py`: SQLite 이력, Knowledge Hub 원문(`hub_replies`), 앱 설정 저장
- `fts_store.py`: SQLite FTS5 검색 인덱스 관리
- `teams_sender.py`: Knowledge Hub 채널 Webhook 전송
- `no_answer_analyzer.py`: 불만족 응답 분석 자동화

### `src/company_llm_rag/data_extraction/`

외부 시스템별 추출기 모음입니다.

- `jira/`: Jira 이슈 및 댓글 수집
- `confluence/`: Confluence 페이지 및 댓글 수집
- `m365/sharepoint_extractor.py`: SharePoint 문서 수집
- `m365/teams_extractor.py`: Teams 채널/채팅 수집, Knowledge Hub Adaptive Card 파싱 및 이미지 다운로드
- `m365/file_parser.py`: 문서 파일 텍스트 추출
- `m365/auth.py`: Microsoft Graph 인증

현재 추출기들은 독립 실행 가능한 스크립트처럼 사용되고, Docker Compose의 `data-loader` 서비스가 이들을 순차 호출합니다.

### `src/company_llm_rag/llm/`

LLM 추상화 계층입니다.

- `base.py`: 공통 인터페이스
- `openai_provider.py`: OpenAI Chat Completions 구현체

OpenAI 호출은 이 레이어를 통해 이뤄지므로, 추후 다른 공급자로 확장할 여지가 있습니다.

### `src/company_llm_rag/prompts/`

기본 시스템 프롬프트와 RAG 지침을 저장합니다.

- `system_prompt.txt`
- `rag_instructions.txt`

환경 변수로 별도 파일을 지정하지 않으면 이 기본 파일을 사용합니다.

### `src/company_llm_rag/templates/`, `static/`

웹 UI 리소스입니다.

- `templates/index.html`: 채팅 화면
- `templates/admin.html`: 어드민 대시보드
- `static/*`: UI 이미지 리소스
- `static/images/`: Teams Knowledge Hub에서 다운로드된 답변 이미지

### `docker/`

컨테이너 실행 관련 파일입니다.

- `Dockerfile`
- `docker-compose.yml`
- `crontab`
- `cron-entrypoint.sh`

### `tests/`

기본 단위 테스트가 들어 있습니다.

- 설정값 테스트
- 청킹/ADF 변환 테스트
- RAG 프롬프트 조립 테스트

다만 일부 테스트 기대값은 현재 설정 기본값과 차이가 있을 수 있으므로, 문서 기준 신뢰 소스는 코드와 `.env.sample` 입니다.

## 현재 프로젝트 특성

이 코드베이스를 읽으며 보인 특징은 다음과 같습니다.

- 웹 서비스와 배치 적재가 한 저장소 안에 함께 있습니다.
- 검색 품질 보완을 위해 벡터 검색만 쓰지 않고 FTS5를 병행합니다.
- 운영 편의 기능이 이미 많습니다.
  - 스트리밍 응답
  - 피드백 수집
  - Teams 문의 전송
  - 어드민 통계
  - 불만족 응답 분석
- 반면 문서와 테스트는 구현을 일부 따라가지 못한 부분이 있어, 유지보수 시에는 코드 기준 검증이 필요합니다.

## 유지보수 시 우선 확인할 파일

기능 수정 범위별로 우선 보면 좋은 파일은 아래와 같습니다.

| 변경 목적 | 먼저 볼 파일 |
|----------|-------------|
| 환경 변수 추가 | `config.py`, `.env.sample` |
| 수집 로직 수정 | `data_extraction/*`, `data_loader.py` |
| 검색 품질 조정 | `retrieval_module.py`, `rag_system.py` |
| 웹 UI/엔드포인트 변경 | `web_app.py`, `templates/*`, `static/*` |
| 운영 통계/이력 변경 | `history_store.py`, `no_answer_analyzer.py` |
| Knowledge Hub 동작 변경 | `teams_extractor.py`, `data_loader.py`, `rag_system.py`, `history_store.py` |
| 배포 방식 변경 | `docker/Dockerfile`, `docker/docker-compose.yml` |
