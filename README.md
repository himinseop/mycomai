# 🚀 Mycomai: 회사 전용 LLM RAG 시스템 운영 가이드

이 프로젝트는 Jira, Confluence, SharePoint, Teams 등 사내 협업 툴의 데이터를 자동으로 수집하고, OpenAI 임베딩을 통해 지식 베이스(ChromaDB)를 구축하여 사내 전용 RAG(Retrieval-Augmented Generation) 시스템을 제공합니다.

## 📋 사전 준비 사항
- **Docker & Docker Compose**가 설치되어 있어야 합니다.
- **Git**이 설치되어 있어야 합니다.
- 각 플랫폼(Atlassian, Microsoft 365)의 **API 접근 권한 및 토큰**이 필요합니다.

## 🛠️ 설치 및 설정

### 1. 코드 내려받기
```bash
git clone https://github.com/himinseop/mycomai.git
cd mycomai
```

### 2. 환경 변수 설정
루트 디렉토리에 `.env` 파일을 생성하고 아래 내용을 입력합니다. (`.env.sample` 참고)
```env
# OpenAI
OPENAI_API_KEY=sk-...

# Atlassian (Jira, Confluence)
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_EMAIL=your-email@company.com
JIRA_API_TOKEN=your-token
CONFLUENCE_BASE_URL=https://your-domain.atlassian.net/wiki
CONFLUENCE_EMAIL=your-email@company.com
CONFLUENCE_API_TOKEN=your-token

# Microsoft 365 (SharePoint, Teams)
TENANT_ID=your-tenant-id
CLIENT_ID=your-client-id
CLIENT_SECRET=your-client-secret

# 선택 사항: 특정 대상만 수집하고 싶을 때 (비워두면 자동 탐색)
JIRA_PROJECT_KEY=
CONFLUENCE_SPACE_KEY=
SHAREPOINT_SITE_NAME=
TEAMS_GROUP_NAME=
```

## ⚙️ 실행 방법

### 1. Docker 이미지 빌드
```bash
docker-compose -f docker/docker-compose.yml build
```

### 2. 최초 전체 데이터 수집 (1회)
권한이 있는 모든 과거 데이터를 가져와 벡터 DB를 구축합니다.
```bash
docker-compose -f docker/docker-compose.yml up data-loader
```
- 실시간으로 `[1/N] Processing...` 메시지가 출력되며 진행 상황을 확인할 수 있습니다.

### 3. 지속적인 증분 수집 (상시 실행)
매일 새벽 2시에 최근 1일치 변경분만 자동으로 업데이트하도록 스케줄러를 실행합니다.
```bash
docker-compose -f docker/docker-compose.yml up -d cron-scheduler
```
- 백그라운드에서 실행되며, 서버가 재시작되어도 자동으로 다시 시작됩니다.

### 4. RAG 시스템 테스트
수집된 데이터를 바탕으로 질문을 던져 답변을 확인합니다.
```bash
docker-compose -f docker/docker-compose.yml run --rm rag-system
```

## 📂 프로젝트 구조
- `src/`: 데이터 추출기 및 RAG 핵심 소스 코드
- `data/`: 추출된 JSONL 데이터 파일 (로그 포함)
- `docker/`: Dockerfile 및 오케스트레이션 설정
- `chroma_db/`: 벡터 데이터베이스 저장소 (영구 보존)

## 📝 참고 사항
- **중복 방지:** `upsert` 로직이 적용되어 있어 동일한 데이터를 여러 번 로드해도 중복되지 않고 최신 상태로 업데이트됩니다.
- **자동 탐색:** 환경 변수에서 프로젝트 키 등을 비워두면 권한이 있는 모든 대상을 자동으로 찾아 수집합니다.
- **증분 수집:** 크론잡 실행 시 `LOOKBACK_DAYS=1`이 적용되어 수집 시간을 최소화합니다.
