# Issue #36 FTS 저장소 분리 설계

## 배경

- 현재 `query_history.db` 하나에 다음 데이터가 함께 저장된다.
  - 질의 이력 `query_history`
  - FTS 인덱스 `doc_fts`
  - 앱 설정 `app_settings`
- 질의 이력과 FTS는 쓰기 패턴과 운영 목적이 다르다.
  - 질의 이력: 웹 요청 시 짧은 INSERT/UPDATE, 운영 조회 대상
  - FTS: 적재 배치 시 대량 upsert/rebuild, 검색 인덱스 전용
- 같은 SQLite 파일을 공유하면 저널 모드, 락 경로, 백업/복구 절차가 함께 묶인다.

## 목표

- `query_history.db` 계열 명칭을 웹 운영 DB 역할에 맞게 재정의한다.
- `doc_fts`를 별도 SQLite DB로 분리한다.
- 운영자가 이력 DB를 단독으로 직접 조회하고 백업할 수 있게 한다.
- FTS 대량 갱신이 이력 DB와 직접 충돌하지 않도록 한다.

## 명칭 개편 방향

현재 `query_history`라는 이름은 "질문 이력"에만 초점이 맞춰져 있어 다음 역할을 충분히 설명하지 못한다.

- 답변 저장
- 피드백 저장
- 분석 결과 저장
- 웹/어드민 운영 데이터 저장

따라서 36번 작업에서는 저장소와 테이블 명칭도 함께 정리하는 것을 권장한다.

### 권장 명칭

- DB 파일
  - 기존: `query_history.db`
  - 권장: `app_data.db`
- 검색 인덱스 DB 파일
  - 기존: `fts.db`
  - 권장: `search_index.db`
- 이력 테이블
  - 기존: `query_history`
  - 권장: `chat_history`

### 명명 의도

- `app_data.db`
  - 애플리케이션 운영 데이터 전반을 담는 이름이다.
  - 향후 피드백, 분석, 설정, 감사 로그 등 테이블이 늘어나도 어색하지 않다.
  - 특정 UI 채널에 종속되지 않아 장기적으로 더 넓게 쓸 수 있다.
- `search_index.db`
  - FTS뿐 아니라 향후 검색용 보조 인덱스나 파생 검색 테이블까지 담을 수 있다.
  - 검색 엔진 내부 저장소라는 역할이 이름에 직접 드러난다.
- `chat_history`
  - 질문뿐 아니라 답변까지 포함하는 실제 저장 내용을 더 잘 설명한다.
  - 웹 UI 기준 대화 이력이라는 의미가 분명하다.

## DB 역할 정의

### `app_data.db` (기존 `query_history.db`)

- 웹 애플리케이션 운영용 단순 DB로 정의한다.
- 주 용도:
  - 사용자 질문/답변 이력 저장
  - 피드백 저장
  - 분석 결과 저장
  - 웹/어드민 화면에서 직접 조회하는 운영 데이터 저장
- 성격:
  - 사람이 직접 조회하거나 백업/복구하기 쉬워야 한다.
  - 테이블 구조가 직관적이어야 한다.
  - 검색 인덱스나 대량 재생성 가능한 파생 데이터는 넣지 않는다.

### `search_index.db`

- 문서 검색 전용 DB로 정의한다.
- 주 용도:
  - FTS 인덱스 `doc_fts`
  - 향후 검색 품질 향상을 위한 인덱스성/파생성 데이터
- 성격:
  - 원본 문서나 ChromaDB에서 재구축 가능한 데이터만 저장한다.
  - 대량 upsert, rebuild, optimize 같은 검색 인덱스 작업을 감당한다.
  - 웹 운영 이력과는 분리된 수명주기와 점검 절차를 가진다.

## 비목표

- ChromaDB 저장 구조 변경
- FTS 검색 문법 자체 변경
- 검색 랭킹 알고리즘 변경

## 대상 구조

### 변경 전

- `db/query_history.db`
  - `query_history`
  - `doc_fts`
  - `app_settings`

### 변경 후

- `db/app_data.db`
  - `chat_history`
  - `app_settings`
- `db/search_index.db`
  - `doc_fts`

## 향후 테이블 추가 원칙

새 테이블이 필요할 때는 먼저 아래 기준으로 어느 DB에 들어가야 하는지 판단한다.

### `app_data.db`에 두어야 하는 경우

- 웹 서비스 운영과 직접 연결된 데이터인 경우
- 사람이 SQL로 직접 조회할 가능성이 높은 경우
- 피드백, 감사 로그, 분석 결과처럼 업무 이력 자체가 원본 데이터인 경우
- 재구축 비용이 크거나, 재구축 자체가 불가능한 경우

예:

- `chat_history`
- `feedback_audit`
- `analysis_jobs`
- `admin_actions`

### `search_index.db`에 두어야 하는 경우

- 검색을 빠르게 하기 위한 인덱스/파생 데이터인 경우
- 원본 문서나 다른 저장소로부터 다시 만들 수 있는 경우
- 대량 갱신, 재생성, optimize 대상인 경우
- 운영자가 직접 읽기보다 검색 엔진 내부용으로 쓰는 경우

예:

- `doc_fts`
- 검색어 정규화 인덱스
- 검색용 n-gram/보조 인덱스
- 랭킹 실험용 임시 인덱스 테이블

### 분리 판단 기준

새 테이블이 아래 성격을 가지면 `app_data.db` 대신 별도 저장소를 우선 검토한다.

- 쓰기량이 많다.
- 재생성 가능하다.
- 검색 성능 최적화 목적이다.
- 운영자가 본파일을 직접 열어보는 대상이 아니다.
- 배치 작업 중 락 영향이 웹 운영 데이터까지 번질 수 있다.

## 설계 방향

### 1. DB 연결 분리

- `history_store.py` 안에서 단일 `_DB_PATH`만 두지 않고 역할별 경로를 분리한다.
- 예시:
  - `APP_DATA_DB_PATH`
  - `SEARCH_INDEX_DB_PATH`
- 연결 캐시도 DB별로 분리한다.
  - 예: `_local.history_con`, `_local.fts_con`

### 2. 책임 분리

- `query_history`, `app_settings` 관련 함수는 history DB만 사용한다.
- `fts_upsert`, `fts_bulk_upsert`, `fts_search`, `fts_exists`, `fts_count`는 FTS DB만 사용한다.
- 구현 위치는 두 가지 선택지가 있다.
  - 선택지 A: `history_store.py` 내부에서 연결만 분리
  - 선택지 B: `fts_store.py` 신규 모듈로 분리
- 권장안: `fts_store.py` 신규 모듈 분리
  - 용도 분리가 코드 구조에도 그대로 반영된다.
  - 후속 유지보수가 쉽다.

### 3. 초기화 및 마이그레이션

- 앱 시작 시 history DB와 FTS DB를 각각 초기화한다.
- FTS DB에는 `doc_fts` 가상 테이블만 생성한다.
- 기존 `query_history.db` 안의 `doc_fts`는 마이그레이션 단계에서 `fts.db`로 복사하거나, 더 단순하게는 `rebuild_fts.py`로 재구축한다.
- 권장안: 운영 반영 시 `fts.db` 신규 생성 후 `rebuild_fts.py`로 전체 재구축
  - 기존 SQLite 내부 테이블 복사보다 절차가 단순하다.
  - 인덱스 일관성을 다시 맞추기 쉽다.

### 4. 설정값

- `.env`에 각 DB 경로를 명시적으로 둔다.
- 예시:
  - `CHROMA_DB_PATH=./db/chroma_db`
  - `APP_DATA_DB_PATH=./db/app_data.db`
  - `SEARCH_INDEX_DB_PATH=./db/search_index.db`
- fallback은 기존 관례를 최대한 유지한다.
  - app data DB 기본값은 `CHROMA_DB_PATH` 상위 디렉토리의 `app_data.db`
  - search index DB 기본값은 같은 디렉토리의 `search_index.db`

### 5. 운영/백업 절차

- 백업 대상을 2개로 분리한다.
  - `db/app_data.db`
  - `db/search_index.db`
- 무결성 점검도 각각 수행한다.
- 조회 편의성은 이력 DB 쪽이 더 좋아진다.
  - 검색 인덱스 쓰기와 무관하게 `app_data.db`만 직접 보면 된다.

## 변경 대상 파일

- `src/company_llm_rag/history_store.py`
- `src/company_llm_rag/retrieval_module.py`
- `src/company_llm_rag/data_loader.py`
- `src/company_llm_rag/rebuild_fts.py`
- `src/company_llm_rag/config.py`
- `README.md`
- `docs/01_system_overview.md`
- `docs/02_operations_guide.md`
- `docs/04_docker_deployment_guide.md`
- 필요 시 `src/company_llm_rag/fts_store.py` 신규 추가

## 마이그레이션 절차

1. 설정값과 연결 모듈을 추가한다.
2. 웹 운영 DB 명칭을 `app_data.db`, 이력 테이블 명칭을 `chat_history`로 변경한다.
3. FTS 관련 함수가 `search_index.db`를 사용하도록 변경한다.
4. 배포 시 `db/app_data.db`, `db/search_index.db` 파일을 생성한다.
5. 기존 `query_history.db`의 이력 데이터를 `app_data.db.chat_history`로 마이그레이션한다.
6. `python -m company_llm_rag.rebuild_fts`를 실행해 `search_index.db`를 재구축한다.
7. 검색/적재 정상 동작을 확인한다.
8. 안정화 후 기존 `query_history.db` 내부 `doc_fts` 의존 코드를 제거한다.

## 검증 항목

- 질의 이력 저장/조회 정상 동작
- FTS 검색 결과가 분리 전과 동일하거나 동등 수준인지 확인
- `rebuild_fts.py`가 `search_index.db`를 기준으로 재구축하는지 확인
- 이력 DB를 `sqlite3 db/app_data.db`로 직접 열었을 때 최신 이력이 보이는지 확인
- FTS 재구축 중에도 이력 저장이 과도하게 막히지 않는지 확인

## 리스크

- 경로 분리 중 설정 누락으로 FTS가 비는 문제
- DB/테이블 rename 중 기존 운영 스크립트나 문서가 깨지는 문제
- `rebuild_fts.py` 재구축 시간 증가
- 운영자가 백업 대상을 하나 더 관리해야 하는 점

## 대응

- 기본 경로 fallback 제공
- 배포 체크리스트에 `search_index.db` 생성 및 재구축 포함
- 운영 문서에 백업/복구 절차를 명시
- 구명칭(`query_history.db`, `query_history`) 참조 코드를 전수 검색해 함께 교체

## 성공 기준

- `app_data.db`에서 검색 인덱스 테이블 의존 없이 이력 조회가 가능하다.
- `app_data.db`, `search_index.db`, `chat_history`라는 이름이 실제 역할을 자연스럽게 설명한다.
- 검색은 `search_index.db`를 통해 정상 동작한다.
- 운영 중 이력 DB와 FTS DB의 역할 구분이 문서와 코드에 명확히 반영된다.
