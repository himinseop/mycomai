# Issue #37 작업 결과 보고서

**제목:** 질문 그룹 단위 세션 재정의 및 피드백/분석 구조 개선
**작업일:** 2026-03-25
**커밋:** `0162edc`

---

## 배경

`session_id`가 브라우저 세션 식별자로 혼용되어, 후속 질문·피드백·분석을 같은 질문 흐름으로 묶어 해석하기 어려웠다. 사용자가 체감하는 단위(카드 1개 = 질문 그룹)와 운영 데이터 단위(턴별 `record_id`)가 불일치하는 문제를 해소하기 위해 전면 재정의했다.

---

## 변경 파일 요약

| 파일 | 변경 내용 |
|---|---|
| `history_store.py` | 스키마 확장, 피드백 함수 분리, 그룹 조회 함수 추가, 기존 데이터 마이그레이션 |
| `web_app.py` | turn_index 계산, feedback scope 분기, 어드민 그룹 API 추가 |
| `no_answer_analyzer.py` | session_id 기준 transcript 분석으로 확장 |
| `templates/index.html` | turn_index 상태 추적, 그룹 피드백 문구·scope 적용 |
| `templates/admin.html` | 턴 뷰 / 그룹 뷰 전환, 그룹 목록·상세 모달 추가 |
| `CLAUDE.md` | 완료 이력 정리, chat_history 스키마 섹션 추가 |

---

## 상세 변경 내용

### 1. 데이터 모델 — `history_store.py`

**신규 컬럼 (chat_history)**

| 컬럼 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `turn_index` | INTEGER | 1 | 질문 그룹 내 몇 번째 턴 |
| `parent_record_id` | INTEGER | NULL | 직전 턴의 `id` |
| `group_feedback` | INTEGER | 0 | 질문 그룹 대표 피드백 (1 / -1 / 0) |
| `group_feedback_at` | TEXT | NULL | 그룹 피드백 입력 시각 |

**신규 함수**

- `save_record_feedback(record_id, rating)` — 단건 턴 피드백 저장
- `save_group_feedback(session_id, rating)` — 동일 session_id 전체 group_feedback 업데이트
- `get_last_turn_in_session(session_id)` — 세션의 마지막 turn_index·id 조회
- `get_session_detail(session_id)` — 그룹 전체 transcript + 분석 결과 반환
- `get_session_groups(...)` — 어드민 그룹 뷰용 session_id 단위 집계 목록
- `_migrate_group_fields(con)` — 기존 데이터에 turn_index / parent_record_id / group_feedback 소급 적용 (idempotent, init_db() 시 자동 실행)

**하위 호환**
`save_feedback()`는 `save_record_feedback()`의 래퍼로 유지하여 기존 호출 코드에 영향 없음.

---

### 2. 백엔드 — `web_app.py`

**turn_index 자동 계산**
`/chat`, `/chat/stream` 모두 요청 처리 시작 전 `get_last_turn_in_session()`을 호출하여 `turn_index`와 `parent_record_id`를 결정한 뒤 `history_save()`에 전달한다.

```
첫 질문:    turn_index=1, parent_record_id=None
후속 질문:  turn_index=N+1, parent_record_id=직전 record_id
```

**`/chat/stream` 메타 이벤트 확장**

```json
{
  "type": "meta",
  "record_id": 89,
  "session_id": "sess-xes7nve",
  "turn_index": 2,
  "is_group_root": false,
  "inquiry_available": true,
  "is_no_answer": false
}
```

**`/feedback` scope 분기**

```
scope=group (기본): save_group_feedback(session_id, rating)
scope=record:       save_record_feedback(record_id, rating)
```

`scope=group`으로 👎 피드백 시 `analyze_bad_feedback(session_id=...)` 도 session_id와 함께 호출된다.

**신규 어드민 API**

- `GET /admin/sessions` — 그룹 목록 (page, page_size, group_feedback, date_from, date_to, q 필터)
- `GET /admin/sessions/{session_id}` — 그룹 상세 (전체 transcript + 분석 결과)

---

### 3. 분석 — `no_answer_analyzer.py`

`analyze_bad_feedback()`에 `session_id` 파라미터를 추가했다. `session_id`가 전달되면 `get_session_detail()`로 그룹 전체 transcript를 조회해 프롬프트의 이전 턴 컨텍스트 섹션을 구성한다. `conversation_history` 직접 전달 방식은 하위 호환을 위해 유지한다.

---

### 4. 프론트엔드 — `templates/index.html`

- 카드 상태에 `ctxTurnIndex`, `ctxLastRecord` 추가
- meta 이벤트 수신 시 `turn_index` 저장
- 피드백 전송 페이로드: `scope: "group"` 추가, `conversation_history` 제거
- 피드백 문구 변경: `도움이 됐나요?` → `이 질문 흐름이 도움이 됐나요?`

---

### 5. 어드민 — `templates/admin.html`

이력 조회 탭에 뷰 전환 버튼(턴 뷰 / 그룹 뷰) 추가.

**그룹 뷰 컬럼**

| 컬럼 | 설명 |
|---|---|
| 마지막 질문 시각 | 그룹 내 최신 턴 기준 |
| 첫 질문 | root_question (turn_index=1) |
| 턴 수 | 그룹 내 총 Q&A 수 |
| 그룹 피드백 | 👍 / 👎 / — |
| 분석 | 완료 / 분석중 / — |

행 클릭 시 그룹 상세 모달 오픈. 전체 transcript(턴별 Q&A), 분석 결과, 그룹 피드백·입력 시각 표시.

---

## 기존 데이터 처리

운영 중인 DB에 신규 컬럼이 추가되면 `init_db()` 시 `_migrate_group_fields()`가 자동 실행된다.

- 같은 `session_id` 내 `created_at` 오름차순으로 `turn_index` 재계산
- 직전 레코드를 `parent_record_id`로 연결
- `feedback` 값이 있는 마지막 턴 기준으로 `group_feedback` 채움
- 이미 처리된 세션(turn_index가 2 이상인 레코드 존재)은 건너뜀

> 주의: 기존 `session_id`가 브라우저 세션 개념으로 사용된 구간은 질문 그룹이 과대 묶일 수 있음. 어드민 화면 해석 시 참고.

---

## 관련 문서

- 설계 문서: `docs/issues/37/design.md`
- 테스트 케이스: `docs/issues/37/checklist.md`
