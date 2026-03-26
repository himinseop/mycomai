# Issue #38 구현 체크리스트

**작업일:** 2026-03-25

---

| # | 항목 | 상태 | 비고 |
|---|---|:---:|---|
| 1 | `LLM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_TEMPERATURE` 설정 추가 | ☐ | 기존 OpenAI 환경변수 fallback 유지 |
| 2 | `Settings.validate()`를 provider-aware 방식으로 개편 | ☐ | 생성용/임베딩용 검증 분리 |
| 3 | `LLMProvider` 인터페이스에 `stream_chat()` 명시 | ☐ | 구현체 계약 일원화 |
| 4 | `openai_compatible_provider.py` 신규 추가 | ☐ | `skt/A.X-4.0-Light` 기본 경로 |
| 5 | `llm/factory.py` 신규 추가 | ☐ | provider 선택 책임 집중 |
| 6 | `rag_system.py`의 OpenAI 직접 의존 제거 | ☐ | factory 기반 `default_llm` 사용 |
| 7 | `no_answer_analyzer.py`의 OpenAI 직접 의존 제거 | ☐ | 동일 |
| 8 | `teams_sender.py`의 OpenAI 직접 의존 제거 | ☐ | 요약 모델도 factory 사용 |
| 9 | 기본 sLLM 모델값을 `skt/A.X-4.0-Light`로 문서화 | ☐ | 설정 예시 포함 |
| 10 | OpenAI 기존 경로 회귀 확인 | ☐ | fallback 및 선택 로직 테스트 |
| 11 | 설정/팩토리 단위 테스트 추가 및 수정 | ☐ | `tests/test_config.py` 포함 |
| 12 | 운영 문서에 생성용 sLLM 설정 예시 추가 | ☐ | README, operations guide |

---

## 수동 검증 항목

| TC | 항목 | 상태 |
|---|---|:---:|
| TC-01 | `LLM_PROVIDER=openai`일 때 기존 OpenAI 생성 경로 정상 동작 | 미수행 |
| TC-02 | `LLM_PROVIDER=openai_compatible`일 때 로컬 endpoint로 요청 전송 | 미수행 |
| TC-03 | `LLM_MODEL=skt/A.X-4.0-Light`로 답변 생성 | 미수행 |
| TC-04 | 스트리밍 응답이 기존 SSE 포맷과 호환 | 미수행 |
| TC-05 | Teams 문의 요약 경로가 동일 provider 체계에서 동작 | 미수행 |
| TC-06 | 임베딩이 OpenAI인 상태에서도 앱 설정 검증이 혼동 없이 통과 | 미수행 |
| TC-07 | 환경변수만 바꿔 OpenAI <-> sLLM 전환 가능 | 미수행 |

## 메모

- 이번 범위는 "생성 모델 전환"입니다.
- 임베딩은 유지하므로 재색인은 필요 없습니다.
- 추후 임베딩 로컬화 이슈는 별도로 분리하는 것이 좋습니다.
