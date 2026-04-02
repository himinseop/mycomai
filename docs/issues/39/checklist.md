# Issue #39 개발 검증 체크리스트 및 테스트 케이스

**작성일:** 2026-03-31

---

## 구현 체크리스트

| # | 항목 | 상태 | 비고 |
|---|---|:---:|---|
| 1 | Confluence `get_space_display_name()`의 `headers` 미정의 버그 수정 | ☐ | 예외 삼킴 없이 정상 조회 또는 명시적 fallback |
| 2 | `LLMProvider` 인터페이스에 `stream_chat()` 계약 추가 | ☐ | sync/stream 구현체 계약 일치 |
| 3 | 상위 계층의 `default_llm._default_model` 직접 참조 제거 | ☐ | 공개 메서드/프로퍼티 또는 timing helper 사용 |
| 4 | `rag_system.py`에서 프롬프트 로딩 로직 분리 | ☐ | prompt loading 전용 모듈화 |
| 5 | `rag_system.py`에서 citation/link builder 로직 분리 | ☐ | Teams/Jira/SharePoint 링크 생성 포함 |
| 6 | `rag_system.py`에서 최신성 질의/기간 정책 분리 | ☐ | recency parsing/filtering 독립 테스트 가능 |
| 7 | Knowledge Hub 직접 응답 판정/intro 생성 로직 분리 | ☐ | direct answer 전용 모듈 |
| 8 | `rag_query()` / `rag_query_stream()` 공통 오케스트레이션 정리 | ☐ | sync/stream 완료 로직 중복 축소 |
| 9 | `history_store.py`를 관심사별 저장소로 분리 | ☐ | chat/session/analysis/hub/settings |
| 10 | 저장소 분리 후 기존 마이그레이션 및 TTL 동작 유지 | ☐ | 기존 DB 호환성 유지 |
| 11 | LLM factory 도입 또는 생성 경로 일원화 | ☐ | `rag_system`, `teams_sender`, `no_answer_analyzer` 공통 |
| 12 | 요약 모델과 Hub intro 모델 선택 정책 일원화 | ☐ | 설정 기반 제어 |
| 13 | `config.py`에서 auth helper / integration / retrieval 정책 책임 분리 | ☐ | 환경변수 인터페이스는 최대한 유지 |
| 14 | SQLite connection factory 또는 helper 도입 | ☐ | `history_store`, `fts_store` 공통 |
| 15 | `rebuild_fts.py`의 재구축 전용 튜닝 경로 유지 | ☐ | 공통화 후에도 `synchronous=OFF` 등 유지 |
| 16 | `BaseExtractor` 기반 공통 실행 템플릿 도입 | ☐ | 타이머, 진행률, emit, 안전 처리 |
| 17 | Jira/Confluence/Teams/SharePoint extractor 점진 전환 | ☐ | 출력 스키마 호환 유지 |
| 18 | Confluence 댓글 수집 N+1 개선 여부 결정 및 반영 | ☐ | 이번 범위 포함 시 구현, 제외 시 문서화 |
| 19 | 관리자/운영 문서에 리팩토링 후 구조 반영 | ☐ | 필요한 경우 project structure, operations guide 업데이트 |
| 20 | 회귀 테스트 결과를 이 문서 또는 결과 문서에 기록 | ☐ | 완료 기준 명확화 |

---

## 수동 검증 체크리스트

| # | 항목 | 상태 | 비고 |
|---|---|:---:|---|
| 1 | `/chat` 비스트리밍 응답 정상 동작 확인 | 미수행 | 답변/참고문서/저장 포함 |
| 2 | `/chat/stream` SSE 응답 정상 동작 확인 | 미수행 | token/done/meta 이벤트 포함 |
| 3 | 답변 없음 시 Teams 문의 유도 문구 동작 확인 | 미수행 | 기존 UX 유지 |
| 4 | Knowledge Hub 직접 응답 경로 동작 확인 | 미수행 | 참고문서 없이 원문 직접 반환 |
| 5 | 일반 RAG 경로에서 참고문서 링크 형식 회귀 없음 확인 | 미수행 | Jira/Confluence/Teams/SharePoint |
| 6 | 관리자 상세에서 `retrieved_docs_json` 기반 검색 결과 표시 확인 | 미수행 | 기존 세션 조회 포함 |
| 7 | 결과분석 생성/조회 동작 확인 | 미수행 | 그룹 분석 포함 |
| 8 | 기존 `app_data.db`, `search_index.db` 데이터로 기동 확인 | 미수행 | 마이그레이션/호환성 |
| 9 | 수집기 JSONL 출력이 기존 적재 파이프라인과 호환되는지 확인 | 미수행 | data loader 회귀 방지 |
| 10 | FTS 재구축 스크립트 정상 동작 확인 | 미수행 | 성능/완료 여부 |

---

## 테스트 케이스

| TC | 구분 | 시나리오 | 기대 결과 |
|---|---|---|---|
| TC-01 | Bugfix | Confluence 스페이스 표시명 조회 수행 | 예외 없이 표시명 조회 또는 space key fallback, silent failure 없음 |
| TC-02 | LLM | `LLMProvider` 구현체가 `stream_chat()` 없이 주입될 경우 | 타입/계약 수준에서 조기 실패하거나 구현 강제 |
| TC-03 | LLM | sync/stream 모두 동일 provider 경로 사용 | `rag_system`, `teams_sender`, `no_answer_analyzer`가 공통 factory/생성 경로 사용 |
| TC-04 | LLM | timing 정보 생성 시 모델명 조회 | 구현체 내부 필드 직접 접근 없이 모델명 기록 |
| TC-05 | RAG | 일반 질문으로 `/chat` 호출 | 답변, 참고문서, history 저장 정상 |
| TC-06 | RAG | 동일 질문으로 `/chat/stream` 호출 | SSE `token -> done -> meta` 흐름 유지 |
| TC-07 | RAG | 직접 응답 조건 충족하는 Knowledge Hub 문서가 1위 | intro + 원문 답변 반환, references는 빈 배열 |
| TC-08 | RAG | Knowledge Hub 문서가 1위지만 2위 대비 우세하지 않음 | 기존 LLM RAG 경로로 fallback |
| TC-09 | RAG | 최신/최근 질의 수행 | recency parsing/filtering 결과가 기존 의도와 동일 |
| TC-10 | RAG | Jira 키 직접 언급 답변 생성 | citation/link 치환 결과가 기존 형식과 호환 |
| TC-11 | Storage | 기존 운영 DB로 앱 기동 | 마이그레이션 정상, 기존 이력/세션 조회 가능 |
| TC-12 | Storage | 질문 저장 후 관리자 상세 조회 | `retrieved_docs_json`, references, perf 정보 정상 조회 |
| TC-13 | Storage | 그룹 피드백 후 결과분석 생성 | analysis status 저장 및 조회 정상 |
| TC-14 | Storage | Knowledge Hub 답변 이력 조회 | 활성 답변/이력 조회 정상, 기존 버전 보존 |
| TC-15 | SQLite | `history_store` / `fts_store` 연결 초기화 | 공통 PRAGMA 정책 적용, 기본 timeout/row_factory 유지 |
| TC-16 | SQLite | `rebuild_fts.py` 실행 | 재구축 성공, 전용 성능 튜닝 유지 |
| TC-17 | Extractor | Jira extractor 실행 | 진행률 로그와 JSONL 스키마가 기존과 호환 |
| TC-18 | Extractor | Confluence extractor 실행 | 페이지/댓글 수집 및 메타데이터 출력 정상 |
| TC-19 | Extractor | Teams extractor 실행 | 일반 Teams + Knowledge Hub 질문/답변 분리 수집 정상 |
| TC-20 | Extractor | SharePoint extractor 실행 | 관련 사이트 수집 및 메타데이터 출력 정상 |
| TC-21 | Regression | `/feedback`, `/history`, `/admin` 경로 확인 | 기존 엔드포인트 동작 회귀 없음 |
| TC-22 | Regression | 참고문서 렌더링 UI 확인 | 링크, 이미지, 관리자 상세 강조 표시 회귀 없음 |
| TC-23 | Regression | 답변 없음 케이스 | references 빈 배열 유지, Teams 문의 버튼 노출 조건 정상 |
| TC-24 | Regression | 기존 환경변수로 기동 | 추가 설정 없이 기존 운영 환경 최대한 호환 |

---

## 권장 실행 순서

1. TC-01 ~ TC-04로 버그/인터페이스 정합성 확인
2. TC-05 ~ TC-10으로 RAG 경로 회귀 확인
3. TC-11 ~ TC-16으로 저장소/SQLite 호환성 확인
4. TC-17 ~ TC-20으로 extractor 회귀 확인
5. TC-21 ~ TC-24로 운영 UI/엔드포인트 최종 회귀 확인

## 메모

- 이번 문서는 리팩토링 자체의 완료 여부뿐 아니라 "기능 회귀 없이 분리가 되었는가"를 판정하기 위한 검증 기준입니다.
- 특히 `rag_system.py` 분해와 `history_store.py` 책임 분리는 회귀 범위가 넓으므로, 관련 TC는 한 번이 아니라 단계별로 반복 수행하는 편이 안전합니다.
