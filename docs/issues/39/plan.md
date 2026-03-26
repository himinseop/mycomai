# Issue #39 리팩토링 후보 재분석 및 공통 모듈화 계획

GitHub Issue: https://github.com/himinseop/mycomai/issues/39

## 배경

최근 코드 변경을 다시 반영해 보면, 이 레포는 이미 몇 가지 구조 개선이 진행된 상태입니다.

- FTS 저장소가 운영 이력 DB와 분리됨
- `llm/` 패키지와 `LLMProvider` 추상화가 도입됨
- 질문 그룹 세션, 관리자 조회, 스트리밍 응답 같은 주요 기능이 누적 구현됨

반면 그 과정에서 공통화가 덜 끝난 부분과 책임이 다시 비대해진 부분도 함께 남아 있습니다.

특히 현재는 다음 성격의 문제가 동시에 존재합니다.

- extractor 계층은 여전히 복붙 기반에 가깝다
- SQLite 연결 정책이 여러 파일에 중복되어 있다
- 설정과 외부 연동 책임이 `config.py`에 과도하게 모여 있다
- `rag_system.py`는 검색 정책, 링크 생성, citation 치환, 응답 후처리까지 함께 담당한다
- LLM 추상화는 도입됐지만 실제 사용처는 여전히 OpenAI 구현체에 직접 결합돼 있다

따라서 이번 문서는 "무엇을 새로 만들 것인가"보다 "이미 정리된 부분을 기준으로 남은 결합을 어디서 끊을 것인가"에 초점을 둡니다.

## 목표

- 현재 코드 기준으로 중복 코드와 책임 혼재 지점을 다시 식별한다.
- 이미 정리된 부분과 아직 미완료인 부분을 구분한다.
- 작은 단계로 나눠 적용 가능한 리팩토링 순서를 제시한다.
- 후속 구현 이슈로 분할 가능한 수준의 계획을 문서화한다.

## 비목표

- 이번 이슈에서 실제 리팩토링 구현 완료
- 검색 품질 자체의 재튜닝
- 데이터 스키마 전면 변경
- UI/디자인 개편
- LLM 모델 전환 이슈 자체 수행

## 현재 상태 재평가

### 이미 정리된 부분

### 1. 저장소 분리

운영 이력과 FTS는 이미 별도 DB로 분리되어 있습니다.

- `history_store.py` → `app_data.db`
- `fts_store.py` → `search_index.db`

즉 "FTS 분리" 자체는 끝난 작업이고, 이제 남은 문제는 "분리된 저장소들 사이의 공통 인프라를 어떻게 줄일 것인가"입니다.

### 2. LLM 패키지 도입

`llm/base.py`와 `llm/openai_provider.py`가 존재하므로, LLM 추상화의 골격은 이미 있습니다.

하지만 현재는 아래처럼 반쪽 상태입니다.

- `LLMProvider`는 `chat()`만 인터페이스에 있고 `stream_chat()`은 계약에 없음
- `rag_system.py`, `no_answer_analyzer.py`는 `default_llm`에 직접 의존
- `teams_sender.py`는 `OpenAIProvider`를 직접 생성

즉 "추상화 도입"은 되었지만 "사용처 decoupling"은 아직 끝나지 않았습니다.

### 3. 세션/관리자 기능 고도화

질문 그룹 세션, 그룹 피드백, 관리자 조회 구조는 이미 상당히 고도화되어 있습니다.

이 말은 반대로, 리팩토링 시 웹 기능 회귀 위험이 높아졌다는 뜻입니다. 따라서 저장소/설정/RAG 분리는 작은 단계로 가야 합니다.

## 현재 문제 요약

### 1. Extractor 공통 베이스가 사실상 미사용 상태

`BaseExtractor`는 존재하지만 실제 extractor들은 이를 거의 활용하지 않습니다.

현재 Confluence, Jira, Teams, SharePoint extractor에는 공통적으로 아래가 반복됩니다.

- `_fmt_elapsed()` 정의
- `_PROGRESS_EVERY` 정의
- `main()` 엔트리포인트 루프
- 대상 목록 순회 후 진행률 로그 출력
- 표준 문서 스키마 dict 생성
- `print(json.dumps(...))` 기반 JSONL 출력

영향:

- 예외 처리 정책을 바꾸려면 여러 파일을 동시에 수정해야 함
- 수집 로깅 포맷이 소스별로 계속 갈라질 수 있음
- 신규 extractor가 들어올수록 중복이 더 쌓임

### 2. SQLite 연결/PRAGMA 정책이 세 군데로 분산됨

현재 SQLite 관련 정책은 최소 세 경로에 흩어져 있습니다.

- `history_store.py`
- `fts_store.py`
- `rebuild_fts.py`

중복되는 성격:

- `sqlite3.connect(...)`
- journal mode 설정
- synchronous 설정
- 연결 초기화/재사용 정책

영향:

- 운영 정책 변경 시 일부만 수정될 위험이 큼
- 테스트에서 공통 대역을 넣기 어려움
- 성능/안정성 관련 설정이 파일별로 조금씩 어긋날 수 있음

### 3. `config.py`의 책임이 여전히 너무 큼

현재 `config.py`는 다음을 동시에 담당합니다.

- 환경변수 로딩
- 경로 계산
- 검색 정책 상수 보관
- 통합 서비스 설정 보관
- 필수값 검증
- Jira/Confluence 인증 헤더 생성

문제는 이 파일이 단순 설정 저장소가 아니라, 외부 서비스 인증 로직 일부까지 품고 있다는 점입니다.

영향:

- 설정 수정과 통합 로직 수정이 같이 움직임
- 단위 테스트에서 일부 설정만 대체하기 어려움
- 작은 오타가 런타임에서 늦게 터짐

### 4. `rag_system.py`가 사실상 복합 서비스가 됨

현재 `rag_system.py`에는 다음 책임이 함께 있습니다.

- 프롬프트 파일 로딩
- metadata JSON 복원
- Teams 딥링크 생성
- 문서 표시명 생성
- citation 치환
- Jira 직접 언급 링크 치환
- 최신성 쿼리 판별 및 기간 정책
- 실제 검색 및 LLM 호출 오케스트레이션

기능 추가가 계속되면서 이 파일은 단순 RAG 조립기보다 "검색 후처리와 응답 렌더링 정책의 집합"에 가까워졌습니다.

영향:

- 변경 포인트가 너무 많아 코드 리뷰 난도가 높음
- 링크 표시 정책 변경이 검색 경로 수정으로 번지기 쉬움
- 테스트 단위를 분리하기 어렵고 회귀 범위가 큼

### 5. LLM 추상화가 불완전함

현재 구조는 "추상 인터페이스 존재"와 "실제 의존 분리 완료" 사이에 있습니다.

남아 있는 문제:

- `LLMProvider`에 `stream_chat()` 계약이 없음
- `default_llm._default_model` 같은 구현체 내부 필드를 상위 계층에서 참조함
- 요약용 LLM과 기본 LLM 생성 방식이 분산되어 있음

이 상태에서는 provider를 바꾸더라도 상위 계층이 구현 세부사항에 계속 노출됩니다.

## 즉시 수정이 필요한 항목

### 1. Confluence 스페이스 표시명 조회 버그

`get_space_display_name()` 내부에서 `headers`를 정의하지 않고 사용합니다.

결과:

- 예외가 함수 내부에서 삼켜짐
- 표시명 조회 실패가 드러나지 않음
- 메타데이터 품질이 낮아짐

### 2. Microsoft 365 설정 검증 속성명 불일치

`validate()`는 `TEAMS_GROUP_NAME`을 참조하지만 실제 속성은 `TEAMS_GROUP_NAMES`입니다.

결과:

- 검증 로직이 의도대로 동작하지 않음
- `validate()` 호출 시 예외 가능성 존재

### 3. Confluence 댓글 수집 N+1 패턴 지속

각 페이지마다 별도 댓글 API를 호출하는 구조가 그대로 남아 있습니다.

이 문제는 성능 이슈이면서 동시에 extractor 공통화와도 맞물립니다. 즉 "나중에"가 아니라 적어도 리팩토링 범위 안에서 같이 다뤄야 할 항목입니다.

## 리팩토링 우선순위

### 1단계. 장애성 버그 및 인터페이스 불일치 정리

범위:

- Confluence `headers` 버그 수정
- `TEAMS_GROUP_NAME` 오타 수정
- `LLMProvider`에 `stream_chat()` 계약 추가
- 상위 계층의 구현체 내부 필드 직접 참조 제거 방향 설계

목표:

- 문서화된 추상화와 실제 코드의 불일치 제거
- 이후 구조 변경 전에 명백한 결함 제거

### 2단계. SQLite 공통 인프라 정리

범위:

- 공통 SQLite connection factory 또는 helper 도입
- `history_store.py`, `fts_store.py`, `rebuild_fts.py`의 초기화 정책 정리
- journal mode / synchronous 정책을 한 군데에서 설명 가능하게 정리

목표:

- DB 관련 운영 정책의 단일 진입점 확보
- 저장소 분리 상태는 유지하되 구현 중복 제거

### 3단계. Extractor 실행 템플릿 공통화

범위:

- `BaseExtractor` 역할 재정의
- 공통 메서드 후보 정리
  - 타이머 시작
  - 진행률 로그
  - 안전한 아이템 처리
  - 표준 스키마 emit
- Jira/Confluence/Teams/SharePoint 순으로 점진 전환

목표:

- 수집 로직의 중복 감소
- 예외 처리와 출력 포맷 일관화

### 4단계. RAG 후처리 레이어 분리

범위:

- 프롬프트 로딩 분리
- citation/link builder 분리
- 문서 표시명 생성 로직 분리
- 최신성 질의 판별/정책 분리

목표:

- `rag_system.py`를 실제 질의 오케스트레이션 중심으로 축소
- 표시 정책과 검색 정책의 결합 완화

### 5단계. LLM 생성 경로 일원화

범위:

- `default_llm` 생성 책임을 factory로 이동하는 방향 검토
- `teams_sender.py`, `no_answer_analyzer.py`, `rag_system.py`가 동일한 생성 경로를 사용하도록 정리
- 요약용 모델 생성도 한 경로에서 제어 가능하게 정리

목표:

- provider 교체 가능성 확보
- 구현체 직접 의존 제거

### 6단계. Config 책임 분리

범위:

- app/core 설정
- integration 설정
- retrieval/rag 정책
- auth helper

위 영역을 논리적으로 분리하되, 외부 환경변수 인터페이스는 최대한 유지합니다.

목표:

- 운영환경 호환성을 깨지 않고 내부 구조만 정리
- 설정과 외부 연동 로직의 결합 완화

## 권장 모듈 구조

### 저장소/인프라

- `storage/sqlite.py`
  - SQLite connection factory
  - PRAGMA 적용
  - 공통 row_factory / timeout 정책

### Extractor

- `data_extraction/base_extractor.py`
  - 실행 템플릿
- `data_extraction/common.py`
  - 공통 emit/metadata helper
- `data_extraction/*`
  - 소스별 변환 로직

### LLM

- `llm/base.py`
  - `chat`, `stream_chat` 계약
- `llm/factory.py`
  - 기본 provider / summarizer 생성
- `llm/openai_provider.py`
  - 구현체

### RAG

- `rag/prompts.py`
  - 프롬프트 파일 로딩
- `rag/citations.py`
  - source label, doc display name, citation 치환
- `rag/link_builders.py`
  - Teams/SharePoint/Jira 링크 조립
- `rag/recency.py`
  - 최신성 질의/기간 정책
- `rag/query_service.py`
  - 실제 검색 및 생성 오케스트레이션

## 작업 분할 제안

후속 구현은 아래 순서가 적절합니다.

1. 버그 수정 및 인터페이스 정합성 이슈
2. SQLite 공통화 이슈
3. Extractor 공통화 이슈
4. RAG 후처리 분리 이슈
5. LLM factory/의존 경계 정리 이슈
6. Config 책임 분리 이슈

## 검증 기준

- 기존 웹 경로(`/chat`, `/chat/stream`, `/feedback`, `/history`, `/admin`)가 유지될 것
- extractor 출력 스키마가 기존 적재 파이프라인과 호환될 것
- `app_data.db` / `search_index.db`의 역할 구분은 유지될 것
- 설정 변경 없이 기존 환경이 최대한 그대로 동작할 것
- LLM provider를 바꾸지 않아도 현재 동작이 회귀하지 않을 것
- 관리 기능과 결과분석 기능이 깨지지 않을 것

## 리스크

- extractor 공통화 중 소스별 예외 규칙이 사라질 수 있음
- RAG 후처리 분리 과정에서 reference 링크 형식이 바뀔 수 있음
- SQLite 공통화 시 rebuild 경로의 성능 튜닝이 약화될 수 있음
- LLM factory 도입 시 요약/분석 경로가 예상과 다르게 엮일 수 있음

따라서 이번 이슈의 구현은 작은 PR 단위로 나누고, 특히 2단계와 4단계는 테스트 보강과 함께 진행하는 것이 적절합니다.

## 결론

현재 기준에서 가장 중요한 포인트는 "이미 부분적으로 도입된 추상화들을 실제 경계 분리까지 마무리하는 것"입니다.

우선순위는 다음과 같습니다.

1. 버그와 인터페이스 불일치 제거
2. SQLite 공통화
3. Extractor 실행 템플릿 적용
4. RAG 후처리 분리
5. LLM 생성 경로 일원화
6. Config 책임 분리

이 순서를 따르면 운영 동작을 크게 흔들지 않으면서도, 현재 코드베이스의 가장 큰 중복과 결합을 단계적으로 줄일 수 있습니다.
