# Mycomai RAG 시스템 리팩토링 계획

- 최초 작성일: 2026-02-23
- 마지막 현행화: 2026-03-19
- 목적: 운영 가능한 수준의 안정적이고 확장 가능한 사내 RAG 시스템 유지

## 현재 상태 요약

### 완료된 항목

- 패키지 구조 정리 및 절대 import 전환
- 중앙화된 설정 관리 (`config.py`)
- 구조화된 로깅 (`logger.py`)
- ChromaDB 관리 모듈 분리 (`database.py`)
- Jira 페이지네이션 수정
- Confluence 페이지네이션 수정
- SharePoint/Teams extractor 리팩토링
- Teams 일반 채팅 수집 지원 (`TEAMS_CHAT_IDS`)
- `content_hash` 기반 임베딩 스킵
- 기본 유닛 테스트 추가 (`tests/`)
- 개발 의존성 분리 (`src/requirements-dev.txt`)

### 아직 남아 있는 핵심 과제

- 외부 API 재시도 로직 표준화
- 타입 힌트 보강 및 정적 검사 도입
- 테스트 커버리지 보강과 실제 실행 검증 자동화
- 보안 민감 정보 마스킹
- 운영 관찰성 개선

## 우선순위별 과제

### 1. 안정성

- `tenacity` 또는 동등한 방식으로 재시도 정책 도입
- 네트워크 오류, 429, 일시적 5xx에 대한 공통 처리
- 커스텀 예외 계층 정리
- extractor별 실패 지점과 재시도 가능 지점 분리

### 2. 타입과 품질

- extractor와 loader 전반의 타입 힌트 보강
- `mypy` 도입
- 포매터와 린터 실행 절차 문서화
- 매직 넘버 상수화

### 3. 테스트

- 설정 파싱 테스트를 현재 기본값 기준으로 보정
- extractor 단위 테스트 추가
- ChromaDB, OpenAI, Graph API mocking 전략 정리
- CI에서 테스트 자동 실행

### 4. 보안

- 인증 정보가 로그에 노출되지 않도록 마스킹
- 환경변수 누락 시 실패 메시지 표준화
- 장기적으로 Secret Manager 검토

### 5. 성능과 운영성

- 수집 단계 병렬화 가능성 검토
- Graph API 호출 공통 유틸 정리
- 메트릭과 수집 로그 구조 개선
- 증분 수집과 전체 수집 운영 절차 분리

## 로드맵

### Phase 1

- 상태: 완료
- 범위:
  - 패키지 구조 정리
  - 설정 중앙화
  - 로깅 정비
  - 의존성 고정

### Phase 2

- 상태: 진행 전
- 범위:
  - 재시도 로직
  - 타입 힌트 보강
  - 테스트 보강
  - CI 기본 파이프라인

### Phase 3

- 상태: 진행 전
- 범위:
  - 성능 최적화
  - 관찰성 도입
  - 운영 자동화 고도화

## 참고 메모

- 현재 문서와 코드 기준 실행 방법은 [`README.md`](/Users/himinseop/Dev/lab/mycomai/README.md)를 우선 참고합니다.
- 페이지네이션 관련 상세 배경은 [`PAGINATION_FIX.md`](/Users/himinseop/Dev/lab/mycomai/PAGINATION_FIX.md)에 정리되어 있습니다.
- 중복 최소화 전략은 [`DEDUPLICATION_STRATEGIES.md`](/Users/himinseop/Dev/lab/mycomai/DEDUPLICATION_STRATEGIES.md)를 참고합니다.
