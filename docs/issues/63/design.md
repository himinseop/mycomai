# #63 검색 품질 고도화 — 설계

> 목표: 답변을 더 구체적·확장된 정보로 제공하면서 할루시네이션을 극단적으로 줄인다.
> 원칙: 측정 가능한 것부터(1단계), 검색 추가 호출·LLM 추가 호출 없이 결정적 조회로 확장(2단계), 답변 후 근거 검증(3단계), 관계 스키마화(4단계).

## 1. 현황 (2026-09-04 코드 기준)

```
질문 재작성(rewrite 역할, gpt-4o-mini)
 → 하이브리드 검색 1회 (벡터 + FTS, 후보 = TOP_K×3)
 → RRF 융합: 매뉴얼 ×DOCS_RRF_BOOST(4.0), 위키 ×3.0, Hub ×5.0 / 다이제스트는 거리 부스트 BOOST_DIGEST(0.85)만
 → 리랭커(bge-reranker-v2-m3, RERANKER_TOP_N=10) + 매뉴얼 리랭크 보정
 → 다이제스트·원본 URL 중복 시 원본 제거, 다이제스트로 대표 교체
 → inject_entity_docs: 엔티티 사전 매칭 시 매뉴얼 청크 + 최근 Jira/Confluence 앞에 주입
 → build_rag_prompt (역할 라벨 헤더) → LLM 1회 (chat 역할, 운영 .env는 gpt-4o-mini)
 → _resolve_citations([REF n]) → 참고문서: 매뉴얼 근거면 provenance(#61 §12), 아니면 검색 결과
```

의도한 흐름(매뉴얼 → 연관 다이제스트 → 원본 → 답변)과의 차이:

| 의도 | 현재 | 원인 |
|---|---|---|
| 매뉴얼 우선 | 부분 충족 | 부스트일 뿐 보장 아님. 질문 표현이 Jira 문장에 더 가까우면 Jira가 1위 |
| 매뉴얼→다이제스트 | 미구현 | `provenance.extract_from_chunks`가 링크를 뽑지만 참고문서 표시에만 사용 |
| 다이제스트→원본 | 미구현(역방향) | `retrieval_module` dedup이 원본을 버림. 원본을 "출처 확인된 보조자료"로 넣는 경로 없음 |
| 답변 구성 | 1회 LLM | 근거 없는 문장을 걸러내는 후처리 없음. `rag_instructions.txt`는 인라인 인용 금지 |

## 2. 단계별 설계

### 2.1 1단계 — 골든 평가셋 + faithfulness 자동 채점 (착수)

**왜 먼저인가**: 현재 품질 판단은 세션 하나씩 눈으로 보는 방식이라, 이후 단계의 변경이 개선인지 회귀인지 알 수 없다.

**데이터 소스** (활성 Hub 답변은 4건뿐이라 매뉴얼 기반이 주가 된다):
| 소스 | 방법 | 목표 수 |
|---|---|---|
| 플랫폼매뉴얼(`platform/features`, `platform/sites`) | 절(heading) 단위로 LLM이 "현장 직원이 물을 법한 질문 + 근거 문장(verbatim) + 핵심 사실" 생성 → 사람 검토 | 40~60 |
| Hub 답변(`hub_replies`, is_active=1) | 질문·답변 그대로 정답 | 4 |
| 실사용 질문(`chat_history`) | 질문만 채택, 정답은 사람이 기입(초기엔 `expected_facts` 공란 = faithfulness만 채점) | 10~20 |
| 거부(abstain) 케이스 | 지식베이스에 없는 정책 질문(예: 입력 포맷 규칙) — 정답은 "확인 불가" 문구 | 5~10 |

**골든셋 형식** `tests/eval/golden.jsonl` (한 줄 = 한 케이스):
```json
{"id":"man-coupon-001","question":"E쿠폰 배달료는 누가 부담해?","category":"manual|hub|live|abstain",
 "expected_facts":["배달료는 업주 부담","2026-06 고도화 이후 적용"],
 "expected_sources":["docs-platform/features/e-coupon.md"],
 "must_abstain":false,"source_excerpt":"...근거 원문...","notes":""}
```

**평가 러너** `scripts/eval_rag.py` (pytest가 아닌 수동/CI 옵션 실행 — LLM 비용):
1. 각 케이스에 `rag_query(question, return_refs=True, _docs_out=docs)` 호출 → 답변·참고문서·검색 컨텍스트 확보
2. LLM-judge(`summarize` 역할 또는 `EVAL_JUDGE_MODEL`, 기본 gpt-4o)로 채점:
   - **faithfulness** (0~1): 답변의 각 주장 문장이 검색 컨텍스트에 근거하는가 — 문장 단위 판정 후 비율. 컨텍스트에 없는 주장 = 할루시네이션 카운트
   - **correctness** (0~1): `expected_facts` 중 답변에 포함된 비율 (facts 공란이면 N/A)
   - **abstain**: `must_abstain`이면 no-answer 문구 여부, 아니면 no-answer가 아닌지
   - **source_hit**: `expected_sources` 중 하나라도 참고문서·컨텍스트에 있는가
3. 결과 `tests/eval/results/{timestamp}.json` + 요약 markdown(케이스별 점수, 실패 사유). `--baseline {file}`로 이전 결과와 비교하여 회귀 케이스 표시
4. 결정성: judge temperature 0, 케이스 순서 고정, 답변은 결과 파일에 보존(재채점 가능)

**주의**:
- 평가는 운영 DB(ChromaDB .110)를 대상으로 한다. 골든셋의 `expected_sources`는 doc_id 접두어로 매칭(청크 id 아님)
- 판정 프롬프트는 `src/company_llm_rag/prompts/eval_judge.txt`로 분리
- 골든셋 생성 스크립트 `scripts/build_golden_set.py`는 초안만 만들고, 채택은 사람이 `golden.jsonl`로 옮기며 검토한다(자동 채택 금지)
- API 비용: 케이스당 답변 1회 + 판정 1~2회. 60케이스 기준 gpt-4o 판정 약 수백 원

**완료 조건**: 골든셋 ≥ 50건(검토 완료 표시), 러너 1회 실행 결과 저장, 베이스라인 수치 이슈 코멘트로 기록.

### 2.2 2단계 — 출처 기반 컨텍스트 확장

검색은 1회 유지. 매뉴얼 근거 판정(`_is_manual_grounded`) 후 결정적 조회로 확장:
```
매뉴얼 청크 → provenance.extract_from_chunks → 다이제스트 relpath, 이슈키
  ├─ 다이제스트: where docs_relpath=… 로 질문 벡터 조회 (1~2청크/다이제스트)
  ├─ 관련 일감(digest_issues, 구현 역할 우선): where jira_issue_key=… (1청크/이슈, ≤N)
  └─ 원본(digest origin URL → original_doc_id): 질문 관련 절 where 조회 (1~2청크)
토큰 예산 내 매뉴얼 > 다이제스트 > 원본 순 적재, 역할 라벨('당시 기획', '세부 스펙 — 시점 주의') 부착
```
- dedup에서 버려진 원본은 검색 결과가 아니라 **출처 확인된 보조자료**로만 재진입
- 구현 위치: `rag/context_expand.py`(신규) + `rag_system`에서 `inject_entity_docs` 다음 단계
- 게이트: `CONTEXT_EXPAND_ENABLED`, 예산 `CONTEXT_EXPAND_MAX_TOKENS`
- 검증: 1단계 러너로 faithfulness·correctness 전후 비교

### 2.3 3단계 — 답변 모델 상향 + 인용 후 검증(cite-then-strip) + abstain 임계치
- chat 역할을 gpt-4o 이상으로(대시보드 LLM 모델 스위치로 전환 가능), mini는 rewrite·judge 용도
- 프롬프트: 문장마다 `[REF n]` 필수 → 후처리에서 (a) 인용 없는 단정문은 제거 또는 "확인 필요"로 완화, (b) 선택적으로 mini judge가 문장-REF 근거 일치 검사(NLI) → (c) 표시 전 인용 마커 제거(기존 `_resolve_citations` 활용)
- abstain: 리랭크 확률 최상값 < 임계치이면 no-answer. 임계치는 1단계 abstain 케이스로 캘리브레이션
- 지연: 검증 호출 1회 추가(약 1초). 스트리밍 경로는 완성 후 검증 결과로 치환

### 2.4 4단계 — 경량 온톨로지
현재 그래프(`nodes/edges`, `entities`, 다이제스트 역할 엣지 구현/후속/원인/미구현)는 초보적 온톨로지. 보강:
- 노드 타입: 기능·정책·사이트·기획문서·일감·매뉴얼절 / 관계 타입: 구현한다·대체한다·상위기능·적용사이트·근거 — `graph/schema.py`에 선언, 미선언 관계 삽입 거부
- 유효 시점: 노드·엣지에 `valid_from/valid_to`, 폐기 표시 → 2단계 확장 시 폐기 문서는 라벨만 바꿔 넣거나 제외
- 관리: 엔티티 사전 UI 확장(관계 편집)
- OWL/RDF 도입은 하지 않음. 속성 그래프 + 스키마 선언으로 충분

## 3. 검증 계획
모든 단계는 1단계 러너의 수치(faithfulness, correctness, abstain 정확도, source_hit)와 지연(retrieval/inject/llm ms)을 전후 비교하여 이슈 코멘트에 기록한다.
