# RAG 평가 (#63 1단계)

`rag_query`의 답변 품질을 골든셋 기준으로 자동 채점합니다. 목적은 이후 검색/프롬프트
변경이 개선인지 회귀인지 수치로 판단하는 것입니다. 설계: `docs/issues/63/design.md` §2.1.

## 파일

| 파일 | 역할 |
|---|---|
| `golden.jsonl` | 평가에 실제 사용하는 골든셋 (한 줄 = 한 케이스) |
| `golden_draft.jsonl` | 생성기 원본 출력(필터 적용 전, 전량) — 사람 검토용 후보 풀 |
| `results/*.json` | 러너 실행 결과(베이스라인으로 커밋) |

케이스 스키마:
```json
{"id": "man-ecoupon-001", "question": "...", "category": "manual|hub|live|abstain",
 "expected_facts": ["..."], "expected_sources": ["docs-platform/features/ecoupon.md"],
 "must_abstain": false, "source_excerpt": "...", "notes": "...", "reviewed": false}
```

## 골든셋 만들기

```bash
# 초안 생성 (LLM 호출, gpt-4o-mini) — golden_draft.jsonl + golden.jsonl(자동 필터 통과분) 생성
python scripts/build_golden_set.py

# 비용/시간 절감을 위한 옵션
python scripts/build_golden_set.py --limit-files 3     # 매뉴얼 파일 수 제한
python scripts/build_golden_set.py --dry-run            # LLM 호출 없이 절 목록·DB 건수만 확인
```

생성 로직:
- **manual**: `platform/features`, `platform/sites` 마크다운을 `##`/`###` 절 단위로 쪼개
  파일마다 고르게(round-robin) 알찬 절부터 최대 `--target-manual-sections`(기본 45)개를
  뽑아 절마다 LLM에게 현장 직원이 물을 법한 질문 1~2개 + 근거 사실 + 원문 발췌를 요청합니다.
- **hub**: `hub_replies`(is_active=1) 4건의 질문·답변을 그대로 채택하고, 답변에서
  핵심 사실만 LLM으로 뽑습니다.
- **live**: `chat_history`의 실사용 질문 중 최근 것부터 정규화 후 중복 제거해 최대
  `--target-live`(기본 15)건. 정답(`expected_facts`)은 비워둡니다 — faithfulness만 채점됩니다.
- **abstain**: 매뉴얼에 명시되지 않은 세부 규정(입력 포맷, 자릿수 제한 등) 질문을
  LLM에게 찾아달라고 요청 — 최대 `--target-abstain`(기본 8)건.

**자동 품질 필터** (`golden_draft.jsonl` → `golden.jsonl`): `source_excerpt`가 실제
절 본문의 부분 문자열이 아니면 제외, 질문 8자 미만 제외, 정규화 후 중복 질문 제외.
필터를 통과해도 `reviewed: false`입니다 — **사람이 최종 검토해 정확하지 않은 케이스를
지우고 `reviewed`를 `true`로 바꿔야 실제 신뢰할 수 있는 골든셋이 됩니다.** 생성기는
채택을 자동화하지 않습니다(design.md §2.1 "자동 채택 금지").

## 평가 실행

```bash
# 전체 골든셋 평가 (rag_query 실행 + LLM-judge 채점)
python scripts/eval_rag.py

# 일부만
python scripts/eval_rag.py --category manual --limit 10
python scripts/eval_rag.py --ids man-ecoupon-001,abstain-coupon-001

# 이전 베이스라인과 비교 (회귀 케이스 표시)
python scripts/eval_rag.py --baseline tests/eval/results/20260901-000000.json

# 검색은 다시 하지 않고 judge 프롬프트/모델만 바꿔 재채점
python scripts/eval_rag.py --rejudge tests/eval/results/20260901-000000.json
```

결과는 `tests/eval/results/{timestamp}.json`에 저장됩니다(답변·컨텍스트·judge 원문까지
전부 보존 — `--rejudge`로 검색 없이 재채점 가능). 의미 있는 실행 결과는 베이스라인으로
그대로 커밋하세요.

## 채점 지표

- **faithfulness** (0~1): 답변의 각 주장 문장이 검색 컨텍스트로 뒷받침되는가. 사실
  주장이 없으면 1.0.
- **correctness** (0~1 또는 N/A): `expected_facts` 중 답변에 포함된 비율. `expected_facts`가
  비어 있으면 N/A(hub 일부·live·abstain 케이스가 여기 해당).
- **abstain 정확도**: `must_abstain` 케이스는 "확인 불가" 문구가 나왔는지, 아니면 그
  문구 없이 답했는지를 모든 케이스에 대해 판정.
- **source_hit**: `expected_sources` 중 하나라도 참고문서·검색 컨텍스트의 원본 문서
  id 접두어와 일치하는가. `expected_sources`가 비어 있으면 N/A.
- 회귀 판정(`--baseline`): faithfulness 0.2 초과 하락, abstain 정답→오답, source_hit
  상실(true→false) 중 하나라도 있으면 회귀로 표시.

## 비용

케이스당 실제 답변 생성 1회(운영 chat 모델) + judge 판정 1회(`EVAL_JUDGE_MODEL`, 기본
gpt-4o, temperature 0). 골든셋 60여 건 기준 judge 비용은 대략 수백 원 수준입니다.
골든셋 생성(초안)은 절/hub 건당 gpt-4o-mini 1회 호출로 비용이 더 낮습니다.

## 테스트

핵심 로직(`src/company_llm_rag/eval/runner.py`)은 순수 함수라 네트워크 없이 검증합니다.

```bash
docker run --rm -v "$PWD/src:/app" -v "$PWD/tests:/app/tests" -v "$PWD/scripts:/app/scripts" \
  -v "$PWD/db:/app/db" -w /app -e PYTHONPATH=/app --env-file .env mycomai-rag \
  sh -c "pip install -q pytest httpx 2>/dev/null; python -m pytest tests/test_eval_runner.py -q"
```
