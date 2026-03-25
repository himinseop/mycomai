# Issue #37 체크리스트 수행 내역

**작업일:** 2026-03-25

---

| # | 항목 | 상태 | 구현 위치 |
|---|---|:---:|---|
| 1 | `chat_history` 마이그레이션 컬럼 추가 | ✅ | `history_store.py` — `_migrate_add_columns()`, `CREATE TABLE` |
| 2 | `turn_index` 계산 로직 추가 | ✅ | `web_app.py` — `/chat`, `/chat/stream` 요청 시 `get_last_turn_in_session()` 호출 후 `+1` 계산 |
| 3 | `parent_record_id` 연결 로직 추가 | ✅ | `web_app.py` — 동일 위치에서 `last_turn["id"]`를 `parent_record_id`로 전달 |
| 4 | 그룹 피드백 저장 로직 추가 | ✅ | `history_store.py` — `save_group_feedback()` 신규 추가 |
| 5 | 기존 턴 피드백 로직과 충돌 없이 동작 | ✅ | `history_store.py` — `save_feedback()`을 `save_record_feedback()` 래퍼로 유지 |
| 6 | `/chat` 응답에 `turn_index` 포함 | ✅ | `web_app.py` — `ChatResponse`에 `turn_index`, `is_group_root` 필드 추가 |
| 7 | `/chat/stream` 메타 이벤트에 `session_id`, `turn_index` 포함 | ✅ | `web_app.py` — `meta_ev`에 `session_id`, `turn_index`, `is_group_root` 추가 |
| 8 | `/feedback`에 `scope`, `session_id` 처리 추가 | ✅ | `web_app.py` — `FeedbackRequest`에 `scope` 필드 추가, `scope=group` / `scope=record` 분기 |
| 9 | 그룹 분석 호출 경로 추가 | ✅ | `web_app.py` — `scope=group` 피드백 시 `analyze_bad_feedback(session_id=...)` 전달 |
| 10 | `analyze_bad_feedback`를 그룹 transcript 기준으로 확장 | ✅ | `no_answer_analyzer.py` — `session_id` 파라미터 추가, `get_session_detail()`로 전체 transcript 구성 |
| 11 | 관리자 그룹 목록 조회 API 추가 | ✅ | `web_app.py` — `GET /admin/sessions` |
| 12 | 관리자 그룹 상세 조회 API 추가 | ✅ | `web_app.py` — `GET /admin/sessions/{session_id}` |
| 13 | 관리자 화면에 그룹 뷰 추가 | ✅ | `templates/admin.html` — 턴 뷰 / 그룹 뷰 전환, 그룹 목록 테이블, 그룹 상세 모달 |
| 14 | 피드백 문구를 질문 그룹 기준으로 수정 | ✅ | `templates/index.html` — `"도움이 됐나요?"` → `"이 질문 흐름이 도움이 됐나요?"` |
| 15 | 기존 데이터 마이그레이션 스크립트 또는 초기화 로직 작성 | ✅ | `history_store.py` — `_migrate_group_fields()`, `init_db()` 호출 시 자동 실행 (idempotent) |
| 16 | 운영 문서 갱신 | ✅ | `CLAUDE.md` — 불필요한 완료 이력 정리, `chat_history` 스키마 섹션 추가 |

---

## 미수행 항목

없음. 전체 16개 항목 완료.

---

## 수동 검증 (TC-01 ~ TC-10)

설계 문서(`docs/issues/37/design.md`) 기준으로 검증을 수행했다.

| TC | 항목 | 상태 |
|---|---|:---:|
| TC-01 | 새 질문 시작 시 새 세션 생성 | 검증 완료 |
| TC-02 | 후속 질문은 같은 세션 유지 | 검증 완료 |
| TC-03 | 다른 새 질문은 다른 세션으로 분리 | 검증 완료 |
| TC-04 | 그룹 피드백 저장 | 검증 완료 |
| TC-05 | 그룹 피드백과 턴 피드백 분리 | 검증 완료 |
| TC-06 | 그룹 분석 수행 | 부분 검증 |
| TC-07 | 관리자 그룹 목록 조회 | 검증 완료 |
| TC-08 | 관리자 그룹 상세 조회 | 검증 완료 |
| TC-09 | 기존 데이터 마이그레이션 | 검증 완료 |
| TC-10 | Teams 문의와 그룹 세션 호환 | 부분 검증 |

## 검증 메모

- TC-01 ~ TC-05, TC-07 ~ TC-09:
  - 임시 SQLite DB를 사용한 실행 검증으로 확인
  - `turn_index`, `parent_record_id`, `group_feedback`, 그룹 집계, 레거시 마이그레이션 모두 통과
- TC-06:
  - `session_id` 기반 transcript 분석 경로는 코드로 확인
  - `no_answer_analyzer.py`에서 `get_session_detail(session_id)`로 이전 턴 문맥을 구성함
  - 외부 LLM 호출까지 포함한 종단 검증은 이번 점검 범위에서 제외
- TC-10:
  - `index.html`에서 같은 카드의 `ctxSessions[ctxId]`를 Teams 문의에 재사용하는 코드 경로 확인
  - 실제 Teams 전송 종단 검증은 외부 연동 필요로 이번 점검 범위에서 제외
