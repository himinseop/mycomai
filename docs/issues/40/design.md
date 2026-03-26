# Issue #40 결과분석 고도화 & 참고문서 품질 개선

GitHub Issue: https://github.com/himinseop/mycomai/issues/40

## 배경

이력조회 그룹 상세와 결과분석 기능을 운영하면서 다음과 같은 문제가 식별되었습니다.

1. **결과분석이 추론 기반**: 재검색 결과와 Q&A 내용만 보고 LLM이 추론하여 분석하므로, "왜 답변이 미흡했는가"에 대한 실질적인 조사 없이 일반적인 설명만 반환됨
2. **참고문서 누락**: `sess-ttel3ul` Q1에서 답변에 언급된 이슈가 참고문서 링크로 제공되지 않는 현상
3. **참고문서 과다**: `sess-ttel3ul` Q2에서 실제 답변에 기여하지 않은 문서들이 다수 참고문서에 포함됨
4. **관련도 낮은 문서 참고문서 포함**: `sess-uda6m67`에서 개인적인 질문으로 답변이 없었음에도 관련없는 참고문서가 제공됨
5. **그룹 상세에 검색 결과 미표시**: 각 질문에서 실제로 어떤 문서들이 조회되었는지 확인할 방법이 없음
6. **결과보고서 하단 검색 결과 표시 불필요**: 모든 보고서 하단에 재검색 결과 테이블이 항상 표시되어 노이즈가 됨

추가로, `sess-uda6m67` 결과분석에서 재검색된 문서가 원본 답변에 사용된 문서보다 관련도가 높은 현상이 발견되어, 두 시점 간 검색 결과 차이를 비교하는 기능도 함께 요구됨.

## 목표

- 결과분석이 원본 참고문서 vs 재검색 결과를 **실제 비교**하여 누락 문서를 식별하도록 강화
- 답변에 실제 인용된(cited) 문서만 참고문서로 제공하고, 비인용 문서는 더 엄격한 기준으로 필터링
- 이력조회 그룹 상세에서 질문별 검색 결과 목록과 관련도를 확인 가능하게 표시
- 결과보고서에서는 재검색 결과 테이블을 제거하고, 발견 사항이 있을 때만 관련 문서 표시
- 답변없음(`is_no_answer`) 시 참고문서를 제공하지 않도록 비스트리밍 경로 처리 수정

## 비목표

- 검색 알고리즘(RRF, 벡터/키워드 가중치) 자체 변경
- 프롬프트 전면 재설계
- ChromaDB 재색인
- 임베딩 모델 변경

## 변경된 파일

| 파일 | 변경 내용 요약 |
|------|--------------|
| `src/company_llm_rag/rag_system.py` | `_build_references()` cited_indices 파라미터 추가, 거리 임계값 0.5→0.3, `rag_query()` is_no_answer 참고문서 처리 수정 |
| `src/company_llm_rag/history_store.py` | `retrieved_docs_json` 컬럼 추가, `save()` 파라미터 추가, `get_session_detail()` 반환값 확장 |
| `src/company_llm_rag/web_app.py` | `_compact_retrieved_docs()` 헬퍼 추가, 두 chat 엔드포인트에서 retrieved_docs DB 저장 |
| `src/company_llm_rag/no_answer_analyzer.py` | 원본 참고문서 비교 로직 추가, 프롬프트 강화, 보고서 형식 변경 (재검색 테이블 제거 + discovery_docs 조건부 표시) |
| `src/company_llm_rag/templates/admin.html` | 그룹 상세에 "🔍 검색 결과" 섹션 추가, `buildRetrievedDocsHtml()` JS 함수 추가 |

## 핵심 설계 결정

### 1. 참고문서 2단계 필터링

**문제**: 기존 `_build_references()`는 `_distance <= 0.5` 조건 하나로만 필터링하므로,
- 답변에서 명시적으로 인용된 문서가 거리 임계값 초과로 제외되는 경우 발생
- 반대로 관련도는 낮지만 0.5 이하인 문서들이 대거 포함되는 경우 발생

**해결**: cited 여부에 따라 두 가지 다른 기준을 적용

```
① cited 문서 (답변에서 [REFn] 또는 Jira 키로 인용된 문서)
   → _distance 무관, 무조건 참고문서 포함

② _injected 문서 (사용자가 쿼리에 Jira 키를 명시하여 직접 조회된 문서)
   → 기존대로 무조건 포함

③ 나머지 (비인용, 비직접조회)
   → _distance <= 0.3 인 경우만 포함 (임계값 0.5 → 0.3으로 강화)
```

`_resolve_citations()`가 답변 텍스트에서 `[REFn]` 마커와 Jira 이슈 키 언급을 파싱하여 `cited: set`을 반환하고, 이를 `_build_references(cited_indices=cited)`로 전달한다.

**is_no_answer 처리 수정**: 스트리밍 경로(`rag_query_stream`)는 이미 `is_no_answer` 시 `references = []`를 반환하고 있었지만, 비스트리밍 경로(`rag_query`)는 누락되어 있었다. 동일하게 처리하도록 수정.

### 2. retrieved_docs_json 저장 (compact 포맷)

**문제**: 각 질문에서 실제로 어떤 문서들이 검색되었는지 DB에 저장되지 않아, 나중에 결과를 조회할 때 재현 불가능.

**해결**: `chat_history` 테이블에 `retrieved_docs_json` 컬럼 추가. 단, 원본 청크 내용(content)은 용량이 크므로 제외하고 메타데이터와 점수만 저장.

compact 포맷:
```json
[
  {
    "source": "jira",
    "title": "WMPO-1234: 일부 타이틀",
    "url": "https://...",
    "_rrf": 0.028,
    "_vector_rank": 0,
    "_keyword_rank": 2,
    "_injected": false,
    "_distance": 0.31
  }
]
```

`web_app.py`의 `_compact_retrieved_docs()` 헬퍼가 전체 문서 리스트에서 위 필드만 추출하여 직렬화한다.

### 3. 결과분석 보고서 형식 변경

**문제**: 기존 보고서는 항상 하단에 "재검색 결과 N건" 테이블을 표시하여, 분석 본문보다 테이블이 더 크게 보이는 경우가 많고 읽기 불편.

**해결**: 기본적으로 재검색 결과 테이블 제거. 단, **원본 답변에서 누락된 관련도 높은 문서**(`discovery_docs`)가 발견된 경우에만 "발견된 관련 문서 N건 (원본 답변에서 누락)" 섹션으로 조건부 표시.

discovery_docs 선별 기준:
- 재검색 결과 중 `_rrf >= _RRF_MAX * 0.10` (관련도 10% 이상)
- 원본 참고문서 URL에 미포함
- `_distance <= 0.35`

### 4. 원본 참고문서 vs 재검색 비교 분석

**문제**: 기존 결과분석 프롬프트는 재검색 결과 목록만 LLM에 전달하여, "원본 답변이 제공한 참고문서 중 어느 것이 실제로 관련도가 낮았는가", "더 좋은 문서가 있었는데 왜 포함되지 않았는가"를 분석하지 못함.

**해결**: `analyze_bad_feedback()`에서 `all_turns_data`에 이미 저장된 원본 참고문서 URL을 수집하고, 재검색 결과와 비교하여 다음 두 가지를 구분:
- **원본에도 포함되고 재검색에서도 상위**: 원본 참고문서가 실제 관련성이 있음을 확인
- **재검색 상위이나 원본에 미포함**: 답변에서 누락된 관련 문서 → `discovery_docs`

이 비교 결과를 `comparison_text`로 구성하여 LLM 프롬프트의 `{comparison}` 플레이스홀더로 전달. LLM은 이를 기반으로 "원본에서 왜 이 문서가 누락되었는가"를 실제 데이터 기반으로 분석.

## 구현 세부사항

### `rag_system.py`

```python
_MAX_REF_DISTANCE = 0.3  # 0.5 → 0.3

def _build_references(retrieved_docs, listing=False, cited_indices=None):
    for i, doc in enumerate(retrieved_docs):  # enumerate 추가
        ...
        is_cited = cited_indices is not None and i in cited_indices
        if not is_cited and not doc.get('_injected', False) \
                and doc.get('_distance', 0.0) > _MAX_REF_DISTANCE:
            continue
        ...
```

`rag_query()` (비스트리밍):
```python
is_no_answer = _NO_ANSWER_PHRASE in llm_response
if is_no_answer:
    references = []
else:
    references = _build_references(retrieved_docs, listing, cited_indices=cited)
```

### `history_store.py`

마이그레이션:
```python
("retrieved_docs_json", "TEXT    DEFAULT NULL"),
```

`get_session_detail()` 반환:
```python
"retrieved_docs": json.loads(r["retrieved_docs_json"]) if r["retrieved_docs_json"] else [],
```

### `web_app.py`

```python
def _compact_retrieved_docs(docs: list) -> list:
    result = []
    for d in docs:
        meta = d.get("metadata", {})
        result.append({
            "source": meta.get("source", ""),
            "title": meta.get("title", "") or "",
            "url": meta.get("url", "") or "",
            "_rrf": d.get("_rrf", 0),
            "_vector_rank": d.get("_vector_rank"),
            "_keyword_rank": d.get("_keyword_rank"),
            "_injected": d.get("_injected", False),
            "_distance": d.get("_distance", 1.0),
        })
    return result
```

`/chat`, `/chat/stream` 모두 `history_save(... retrieved_docs=_compact_retrieved_docs(docs_holder))` 전달.

### `no_answer_analyzer.py`

`analyze_bad_feedback()` 내 비교 로직 핵심:
```python
_RRF_THRESHOLD = _RRF_MAX * 0.10

for doc in docs:  # 재검색 결과
    rrf = doc.get("_rrf", 0)
    if rrf < _RRF_THRESHOLD:
        continue
    url = meta.get("url", "")
    if url and url in original_ref_urls:
        in_original.append(entry)     # 원본에도 있었던 문서
    elif dist <= 0.35:
        missed_in_original.append(entry)
        discovery_docs.append(doc)    # 원본에서 누락된 문서
```

보고서 HTML:
```python
html = '<div>...llm_html...</div>'
if discovery_docs:
    html += '<div>발견된 관련 문서 N건 (원본 답변에서 누락)...' + discovery_html
```

### `admin.html`

그룹 상세 각 턴 카드에 참고문서 섹션 바로 아래 추가:
```javascript
<div class="sec-hd" onclick="toggleSec(this)">
  🔍 검색 결과 <N건>
</div>
<div class="sec-bd">
  ${buildRetrievedDocsHtml(t.retrieved_docs||[], refUrls)}
</div>
```

`buildRetrievedDocsHtml(docs, refUrls)`:
- 소스 배지 (J/C/S/T 색상 구분)
- 관련도 바 (RRF % 기준, RRF_MAX = 2/61)
- 벡터 순위, 키워드 순위, 직접조회 여부
- `refUrls`에 포함된 문서(참고문서로 선택된 항목) → 연초록 배경 + "참고문서" 배지

## 수용 기준

- 개인적인 질문 → 답변없음 + 참고문서 0건
- 일반 질문 → 답변에서 `[REFn]` 또는 Jira 키로 인용된 문서만 참고문서에 포함
- 관련도가 낮은(distance > 0.3) 비인용 문서는 참고문서에서 제외
- 이력조회 그룹 상세에서 각 질문 카드에 "🔍 검색 결과" 섹션 표시
- 결과분석 보고서 하단에 재검색 결과 테이블이 기본으로 표시되지 않음
- 원본 답변에서 누락된 관련 문서가 있으면 보고서에 "발견된 관련 문서" 섹션 표시

## 알려진 한계 / 향후 개선 후보

### 1. 기존 이력 데이터의 retrieved_docs_json 부재

이번 변경 이후부터 저장되므로, 이전 세션의 그룹 상세에서는 "🔍 검색 결과" 섹션이 빈 상태로 표시됨. 빈 경우 UI에서 "이전 대화에서는 검색 결과를 저장하지 않았습니다" 안내 문구 추가 검토 가능.

### 2. distance 임계값 0.3의 적절성

0.3은 경험적 값이며, 데이터 특성(임베딩 모델, 도메인 특수성)에 따라 조정이 필요할 수 있음. 향후 피드백 데이터 기반으로 최적값을 재검토 필요.

### 3. 결과분석의 keyword-level 검색 부재

현재 분석은 단일 combined 쿼리 재검색 결과를 사용함. 질문의 개별 핵심 키워드별로 별도 검색을 수행하면 "어떤 키워드에 대한 데이터가 KB에 존재하는가"를 더 정밀하게 파악 가능. 다만 여러 번의 DB 호출이 필요하므로 성능 트레이드오프 검토 후 적용 필요.

### 4. discovery_docs 거리 임계값 0.35 조정

원본에서 누락된 관련 문서를 식별하는 기준(distance ≤ 0.35)도 경험적 값이므로, 운영 중 발견되는 케이스를 기반으로 조정 가능.
