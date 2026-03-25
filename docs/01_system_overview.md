# 01. 시스템 소개

## 개요

Mycomai는 사내 문서와 대화 데이터를 수집해 검색 가능한 지식베이스로 만들고, 검색 결과를 바탕으로 답변을 생성하는 사내용 RAG 시스템입니다.

지원하는 주요 데이터 소스는 다음과 같습니다.

- Jira 이슈와 댓글
- Confluence 페이지와 댓글
- SharePoint 문서
- Teams 채널 메시지와 일반 채팅

시스템은 단순 벡터 검색만 사용하지 않고, SQLite FTS5 키워드 검색을 함께 사용해 하이브리드 검색 품질을 높입니다.

## 현재 아키텍처

```text
[외부 시스템]
Jira / Confluence / SharePoint / Teams
        |
        v
[추출기]
src/company_llm_rag/data_extraction/*
        |
        v
[중간 산출물]
data/*.jsonl
        |
        v
[적재기]
data_loader.py
 - SQL 블록 제거
 - 토큰 기반 청킹
 - 콘텐츠 해시 비교
 - ChromaDB upsert
 - FTS5 동기화
        |
        +------------------> db/chroma_db
        |
        +------------------> db/query_history.db
                               - query_history
                               - doc_fts
                               - app_settings
        |
        v
[검색]
retrieval_module.py
 - 벡터 검색
 - FTS5 검색
 - RRF 재정렬
 - 소스/확장자 필터
        |
        v
[응답 생성]
rag_system.py
 - 프롬프트 조립
 - OpenAI Chat Completions 호출
 - 참고문서 링크 구성
        |
        v
[서비스 레이어]
web_app.py
 - 채팅 API
 - 스트리밍 응답
 - 피드백 수집
 - Teams 문의
 - 어드민 대시보드
```

## 핵심 구성 요소

| 모듈 | 역할 |
|------|------|
| `config.py` | `.env` 기반 전역 설정 로드 |
| `database.py` | ChromaDB `PersistentClient` 및 컬렉션 관리 |
| `data_loader.py` | JSONL 문서 청킹, 임베딩, upsert, FTS 동기화 |
| `retrieval_module.py` | 벡터 검색 + FTS5 검색 + RRF 랭킹 |
| `rag_system.py` | 검색, 프롬프트 생성, 답변/참고문서 생성 |
| `web_app.py` | FastAPI 웹 애플리케이션 |
| `history_store.py` | 질의 이력, 피드백, FTS5, 설정 저장 |
| `teams_sender.py` | 답변 부족/불만족 피드백을 Teams로 전달 |
| `no_answer_analyzer.py` | 불만족 응답 분석 결과 생성 |

## 데이터 적재 방식

현재 적재 파이프라인의 특징은 다음과 같습니다.

- 추출기는 소스별로 JSONL을 생성합니다.
- 적재기는 텍스트를 토큰 기준으로 청킹합니다.
- SQL 코드 블록과 SQL 문장 패턴을 제거합니다.
- 기존 청크와 콘텐츠 해시를 비교해 변경분만 다시 임베딩합니다.
- ChromaDB upsert 후 SQLite FTS5 인덱스를 함께 갱신합니다.

즉, 전체 재수집이 가능하면서도 실제 임베딩 비용은 변경분 위주로 줄이도록 설계되어 있습니다.

## 검색 방식

검색은 `retrieval_module.py` 에서 처리합니다.

1. ChromaDB 벡터 검색을 수행합니다.
2. 쿼리에서 핵심 키워드를 추출해 SQLite FTS5 검색을 수행합니다.
3. 두 결과를 RRF(Reciprocal Rank Fusion)로 합칩니다.
4. Jira/Teams/Confluence/SharePoint별 가중치를 적용합니다.
5. 쿼리에 Jira 이슈 키가 있으면 직접 조회 결과를 앞쪽에 주입합니다.
6. 필요 시 소스 필터와 파일 확장자 필터를 적용합니다.

이 구조 덕분에 자연어 질문과 정확한 키워드 검색을 둘 다 어느 정도 커버할 수 있습니다.

## 웹 애플리케이션 기능

`web_app.py` 기준으로 현재 제공하는 기능은 다음과 같습니다.

- `/`: 채팅 UI
- `/chat`: 일반 질의응답
- `/chat/stream`: SSE 스트리밍 응답
- `/feedback`: 답변 만족/불만족 저장
- `/inquiry`: Teams 문의 전송
- `/history/{session_id}`: 세션 이력 조회
- `/admin`: 기본 인증 기반 어드민 대시보드
- `/admin/db-stats`, `/admin/stats`, `/admin/history/data`: 운영 통계 API

## 운영 시 기억할 점

- ChromaDB 경로 기본값은 `./db/chroma_db` 입니다.
- 질의 이력 DB는 `CHROMA_DB_PATH` 상위 디렉토리의 `query_history.db` 를 사용합니다.
- Docker Compose 기준 `web` 서비스가 실제 웹 서버이고, `rag-system` 서비스는 CLI형 엔트리포인트입니다.
- 자동 증분 수집은 `cron-scheduler` 컨테이너와 `docker/crontab` 으로 구성됩니다.
