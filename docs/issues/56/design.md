# Issue #56 도메인별 LLM 인사이트 API — 내부 솔루션용 (1차: 매출 분석)

GitHub Issue: https://github.com/himinseop/mycomai/issues/56

> **진행 상황 (2026-07-05)**: Phase 1~3 구현 완료 (`feature/56-insight-api`).
> - Phase 1 ✅ 인증(API Key+scope+IP) / sales 도메인 / 호출 이력 — 테스트 17건 + 실서버 E2E
> - Phase 2 ✅ 관리자 API 탭(키 발급/차단, 이력 조회) / rate limit(429 이력 기록)
> - Phase 3 ✅ voc 도메인 추가로 레지스트리 확장 검증 — 파일 1개+등록+프롬프트만으로 완료
> - 구현 중 발견·반영: LLM 한글 금액 단위 변환 오류 → `*_display` 문자열을 서버가 확정
>   (모든 금액·증감률 표기는 서버 포맷값 사용). VOC 샘플 원문은 응답/이력에 비노출.
>
> **설계 변경 (2026-07-05)**: 도메인별 경로 → **단일 엔드포인트 `POST /api/v1/insights`**.
> 서버가 요청 데이터를 근거로 도메인 프롬프트를 자동 선택한다.
> - 선택 순서: payload `domain` 명시(explicit) > records 구조 감지(structure,
>   도메인별 signature_fields 커버리지 ≥0.8 단독) > LLM 분류(llm, 질문+필드+샘플 2행)
> - `question`(자연어) 필드 신설 — 분류와 해석 프롬프트에 모두 반영
> - `period` 생략 시 records의 date 범위로 자동 추론
> - scope는 자동 선택된 도메인 기준 검사, `*` = 전체 도메인 허용
> - 응답·이력에 `domain_selection`(explicit|structure|llm) 기록
>
> **보류 (2026-07-05)**: API 대화 세션화·이어질문(지식허브 질문방 연계 포함)은
> 검토만 완료하고 보류. API는 무상태 유지.

## 배경

사내 다른 솔루션들이 이 프로젝트의 LLM 역량을 API로 활용하고 싶어 한다. 첫 수요는 **기간별 매출 데이터 분석**: 매출 데이터를 보내면 (1) 데이터 자체를 요약 설명하고 (2) 눈여겨볼 포인트를 짚어주는 것.

범용 LLM 프록시(아무 프롬프트나 전달)는 다음 이유로 배제한다.

- 호출측마다 프롬프트 품질이 제각각 → 결과 품질 보장 불가
- 비용·오남용 통제 어려움
- 도메인 지식(용어, 지표 해석 기준)을 서버에 축적할 수 없음

대신 **도메인별 전용 엔드포인트**를 제공한다. 도메인마다 입력 스키마·프롬프트·응답 구조를 서버가 소유하고, 호출측은 데이터만 보낸다.

## 목표

- `POST /api/v1/insights/sales` — 기간별 매출 데이터 분석 API (1차 도메인)
- API Key 기반 내부 전용 인증 (퍼블릭 오픈 아님)
- 모든 호출 이력을 DB에 기록하고 관리자 화면에서 조회
- 도메인 추가가 쉬운 레지스트리 구조 (2차: VOC/피드백 분석 등)

## 비목표

- 퍼블릭 API / 외부 과금
- 범용 chat/completion 프록시 엔드포인트
- RAG 검색과의 결합 (인사이트 API는 전달받은 데이터만 분석; 지식베이스 검색은 기존 `/chat`)
- OAuth 등 무거운 인증 체계 (내부망 + API Key로 충분)

## 핵심 설계 방향

### 1. 도메인 레지스트리 구조

도메인 = **입력 스키마 + 전처리(통계 선계산) + 프롬프트 + 응답 구조** 묶음.

```
src/company_llm_rag/insight_api/
├── router.py          # FastAPI APIRouter (/api/v1/insights/{domain})
├── auth.py            # API Key 인증 + scope 검사
├── store.py           # api_clients / api_call_history SQLite 저장소
├── domains/
│   ├── __init__.py    # DOMAIN_REGISTRY = {"sales": SalesDomain(), ...}
│   ├── base.py        # InsightDomain 추상 클래스
│   └── sales.py       # 매출 분석 도메인 (1차)
└── schemas.py         # 공통 요청/응답 Pydantic 모델
```

```python
class InsightDomain(ABC):
    name: str                      # "sales"
    request_model: type[BaseModel] # 도메인별 입력 스키마
    def preprocess(self, req) -> dict: ...   # 결정적 통계 선계산
    def build_prompt(self, req, stats) -> list[dict]: ...  # messages
    def parse_response(self, raw: str) -> dict: ...        # 구조화 응답
```

새 도메인 추가 = `domains/`에 파일 1개 + 레지스트리 등록 + 프롬프트 파일. 라우터/인증/로깅은 공통.

### 2. 수치는 Python이 계산, LLM은 해석만

LLM에 원시 데이터를 주고 "합계 내라" 하면 수치 할루시네이션이 발생한다. 대신:

1. **preprocess 단계**에서 서버가 결정적으로 계산: 총매출, 일/주/월 추이, 전기 대비 증감률, 상·하위 항목, 이상치(z-score 또는 IQR), 요일 패턴 등
2. 계산된 통계표 + 원본 요약을 프롬프트에 주입
3. LLM은 **해석·설명·포인트 도출만** 담당

→ 응답의 모든 수치는 서버 계산값. LLM이 새 숫자를 만들지 않도록 프롬프트에 명시.

### 3. API 명세 (1차: sales)

#### 요청

```
POST /api/v1/insights/sales
X-API-Key: <발급된 키>
Content-Type: application/json
```

```json
{
  "period": {"from": "2026-06-01", "to": "2026-06-30", "granularity": "daily"},
  "compare_period": {"from": "2026-05-01", "to": "2026-05-31"},
  "records": [
    {"date": "2026-06-01", "amount": 1250000, "count": 42, "dimension": {"store": "강남점", "channel": "제로페이"}}
  ],
  "options": {"focus": ["채널별", "요일별"], "language": "ko"}
}
```

- `records[].dimension`: 자유 차원(매장/채널/카테고리 등) — 서버가 차원별 집계 자동 수행
- `compare_period` + 해당 기간 records 포함 시 전기 대비 분석 활성화
- 입력 상한: records 최대 10,000행, payload 최대 5MB (초과 시 422)

#### 응답

```json
{
  "domain": "sales",
  "summary": "6월 총매출은 3.2억 원으로 5월 대비 12% 증가했습니다. ...",
  "highlights": [
    {"type": "growth", "title": "제로페이 채널 급성장", "detail": "...", "evidence": {"metric": "channel_growth", "value": 0.34}}
  ],
  "anomalies": [
    {"date": "2026-06-15", "detail": "일매출이 평균 대비 3.1σ 급감 (전산 장애 여부 확인 필요)"}
  ],
  "stats": { "total": 320000000, "prev_total": 285000000, "growth": 0.123, "...": "서버 계산 통계 원본" },
  "meta": {"model": "gpt-4o-mini", "latency_ms": 2100, "request_id": "req-xxxx"}
}
```

- `stats`는 서버 계산값 그대로 반환 → 호출측이 수치 검증/재활용 가능
- `evidence`로 각 하이라이트가 어떤 계산 지표에 근거했는지 연결

#### 오류

| 코드 | 상황 |
|---|---|
| 401 | API Key 없음/무효 |
| 403 | 해당 도메인 scope 없음 |
| 422 | 입력 스키마 위반, 상한 초과 |
| 429 | rate limit 초과 (Phase 2) |
| 502 | LLM 호출 실패 (재시도 1회 후) |

### 4. 보안 (내부 전용)

| 장치 | 내용 | 단계 |
|---|---|---|
| API Key 인증 | `X-API-Key` 헤더. 키는 **SHA-256 해시로만 저장**(원문은 발급 시 1회 표시). 클라이언트별 발급/폐기 | P1 |
| 도메인 scope | 클라이언트별 허용 도메인 목록 (`sales,voc`) — 발급 시 지정 | P1 |
| 비활성화 | `is_active=0`으로 즉시 차단 (삭제 없이 이력 보존) | P1 |
| IP allowlist | `API_ALLOWED_IPS` env (비우면 미적용). 내부망 대역만 허용 | P1 (env만) |
| Rate limit | 키별 분당 호출 수 제한 (기본 30/min, 클라이언트별 오버라이드) | P2 |
| 페이로드 로깅 최소화 | 이력에는 요청 **요약본**(행수·기간·차원 키)만 저장, 원본 매출 데이터는 저장하지 않음 (민감 데이터 보호) | P1 |

키 발급은 Phase 1에서는 CLI 스크립트(`scripts/api_key_issue.py`), Phase 2에서 관리자 UI.

### 5. 데이터 모델 (app_data.db)

#### api_clients

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT | 클라이언트명 (예: "매출대시보드") |
| `key_hash` | TEXT UNIQUE | API Key SHA-256 |
| `scopes` | TEXT | 허용 도메인 CSV (`sales,voc`) |
| `rate_limit_per_min` | INTEGER | NULL=기본값 |
| `is_active` | INTEGER | 1=활성 |
| `created_at` | TEXT | ISO8601 |

#### api_call_history

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | INTEGER PK | |
| `request_id` | TEXT | 응답 meta와 동일 (추적용) |
| `client_id` | INTEGER | FK → api_clients |
| `domain` | TEXT | "sales" |
| `status` | INTEGER | HTTP 상태코드 |
| `request_summary` | TEXT | JSON: 기간·행수·차원 키·옵션 (원본 데이터 제외) |
| `response_summary` | TEXT | JSON: summary 앞 200자, highlight 수 |
| `model` | TEXT | 사용 LLM |
| `prompt_tokens` / `completion_tokens` | INTEGER | 사용량 (제공 시) |
| `latency_ms` | INTEGER | 총 처리 시간 |
| `error` | TEXT | 실패 시 사유 |
| `created_at` | TEXT | ISO8601 |

### 6. 프롬프트 구성 (sales 도메인)

`prompts/insights/sales.txt` (외부 파일 — 기존 프롬프트 관리 방식과 동일):

```
당신은 소상공인/가맹 사업 매출 데이터를 분석하는 시니어 데이터 애널리스트입니다.

[입력]
- 서버가 계산한 통계표(JSON)와 기간 정보가 주어집니다.

[규칙]
1. 모든 수치는 주어진 통계표의 값만 사용하세요. 새로운 수치를 계산하거나 추정하지 마세요.
2. summary: 기간·총매출·전기 대비·전반적 추이를 사실 위주로 3~5문장 요약.
3. highlights: 눈여겨볼 포인트 2~5개. 급증/급감, 채널·매장 간 편차, 요일/시즌 패턴,
   상·하위 기여 항목 등. 각 항목에 근거 지표(evidence)를 명시.
4. anomalies: 통계표의 이상치 목록을 해석 (원인 후보 제시는 "확인 필요" 톤으로, 단정 금지).
5. 실행 제안은 데이터에서 직접 도출 가능한 범위만. 과도한 일반론 금지.
6. 반드시 지정된 JSON 스키마로만 출력.
```

모델: 기본 `OPENAI_SUMMARIZE_MODEL`(gpt-4o-mini) — 통계 해석은 경량 모델로 충분, 비용 절감. `INSIGHT_LLM_MODEL` env로 오버라이드 가능. 기존 `llm/factory.py` 재사용 (Ollama 전환 구조 그대로 상속).

## 단계별 계획

### Phase 1 — MVP (인증 + sales + 이력)

1. `insight_api/` 패키지 + 라우터 마운트 (`web_app.py`에 `app.include_router`)
2. `api_clients`/`api_call_history` 테이블 + store 함수
3. API Key 인증 미들웨어 (`X-API-Key` → 해시 매칭 → scope 검사), IP allowlist(env)
4. `scripts/api_key_issue.py` — 키 발급/폐기/목록 CLI
5. sales 도메인: 입력 스키마 → 통계 선계산(preprocess) → 프롬프트 → 구조화 JSON 응답
6. 호출 이력 기록 (성공/실패 모두)
7. 테스트: 인증(401/403), 스키마(422), 통계 계산 단위 테스트, LLM mock 응답 파싱

### Phase 2 — 운영성

1. 관리자 탭: API 클라이언트 관리(발급/폐기), 호출 이력 조회(필터: 클라이언트/도메인/기간)
2. Rate limit (키별 분당 제한, 429)
3. 토큰 사용량 집계 대시보드 (클라이언트별 일/월)

### Phase 3 — 도메인 확장

1. 2번째 도메인 추가로 레지스트리 구조 검증 (후보: VOC/피드백 분석, 정산 데이터 설명)
2. (선택) 스트리밍 응답 옵션 (`Accept: text/event-stream`)
3. (선택) 비동기 처리 — 대용량 데이터 job 방식 (`202 + polling`)

## 검증 시나리오 (Phase 1 TC)

| # | 시나리오 | 기대 결과 |
|---|---|---|
| 1 | 키 없이 호출 | 401 |
| 2 | 무효/비활성 키 | 401 |
| 3 | scope에 없는 도메인 호출 | 403 |
| 4 | 정상 매출 데이터 (단일 기간) | 200, summary/highlights/stats 반환, 수치=서버 계산값 일치 |
| 5 | compare_period 포함 | growth 지표 포함, 전기 대비 서술 |
| 6 | dimension 포함 (매장/채널) | 차원별 집계 + 편차 하이라이트 |
| 7 | records 0행 / 상한 초과 | 422 |
| 8 | 날짜 역전·형식 오류 | 422 |
| 9 | LLM 실패 (mock) | 1회 재시도 후 502, 이력에 error 기록 |
| 10 | 모든 호출 후 | api_call_history에 기록 존재, 원본 매출 데이터 미저장 확인 |
| 11 | 이상치 포함 데이터 | anomalies에 해당 일자 포함 |
| 12 | IP allowlist 설정 + 비허용 IP | 403 |

## 리스크 및 대응

| 리스크 | 대응 |
|---|---|
| LLM이 통계표에 없는 수치 생성 | 프롬프트 규칙 + 응답 검증(수치 evidence 매핑 확인), stats 원본 동봉으로 호출측 검증 가능 |
| 대용량 records로 프롬프트 초과 | 원시 행은 프롬프트에 넣지 않음(통계표만). 행수 상한 10,000 |
| web 컨테이너 메모리 영향 | 인사이트 API는 검색/임베딩 미사용 — LLM 호출만이라 영향 미미. records 파싱은 스트리밍 불필요(5MB 상한) |
| 키 유출 | 해시 저장 + 즉시 비활성화 + (P2) rate limit, 이력으로 사용처 추적 |

## 관련 파일 (예정)

| 파일 | 역할 |
|------|------|
| `insight_api/router.py` | `/api/v1/insights/{domain}` 라우팅 + 이력 기록 |
| `insight_api/auth.py` | API Key 인증, scope, IP allowlist |
| `insight_api/store.py` | api_clients/api_call_history 저장소 |
| `insight_api/domains/sales.py` | 매출 통계 선계산 + 프롬프트 + 응답 파싱 |
| `prompts/insights/sales.txt` | 매출 도메인 프롬프트 |
| `scripts/api_key_issue.py` | 키 발급/폐기 CLI |
| `config.py` | `INSIGHT_API_ENABLED`, `INSIGHT_LLM_MODEL`, `API_ALLOWED_IPS` |
