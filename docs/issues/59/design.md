# Issue #59 GraphRAG 도입 — 엔티티·관계 기반 멀티홉/종합 질문 대응

GitHub Issue: https://github.com/himinseop/mycomai/issues/59
선행: #58 LLM 위키 (토픽 수요 데이터·합성 인프라 재사용)

## 배경

현행 파이프라인(하이브리드 검색 + 리랭커 + 청크 RAG)의 구조적 한계:

| 질문 유형 | 현행 동작 | 문제 |
|---|---|---|
| 단일 사실 ("정산 주기는?") | 청크 검색 → 답변 | ✅ 잘함 |
| 멀티홉 ("포인트 정책 변경이 정산에 미치는 영향?") | 각 키워드 청크만 검색 | 관계를 못 탐 |
| 집계 ("최근 쿠폰 관련 이슈 뭐 있었어?") | 상위 K 청크만 | 전체를 못 봄 (top-K 한계) |
| 엔티티 전체 조회 ("위메프오플러스 정책 전부") | 상위 K 청크만 | 커버리지 부족 |

GraphRAG의 핵심: 문서에서 **엔티티·관계 그래프**를 만들고, 질문 유형에 따라
그래프 이웃 탐색(local) 또는 클러스터 요약(global)으로 컨텍스트를 구성한다.

## 설계 원칙: 풀 GraphRAG 배제, 단계적 도입

Microsoft GraphRAG 방식(전 코퍼스 LLM 추출 + Leiden 클러스터링 + 커뮤니티 요약)은
16만+ 청크 기준 **수십만 LLM 호출**(초기 구축) + 증분 유지보수 부담이 커서 배제한다.
대신 "이미 구조화된 데이터 → 빈발 엔티티 → (효과 검증 후) 요약" 순서로 투자한다.

## Phase 1 — 공짜 그래프: Jira 구조 활용

**Jira는 LLM 추출 없이 이미 그래프다.** 수집 JSONL에 있는 필드만으로:

```
(Project)-[HAS_ISSUE]->(Issue)-[LINKS_TO]->(Issue)      # 이슈링크 (blocks/relates)
(Issue)-[ASSIGNED_TO]->(Person)
(Issue)-[HAS_COMPONENT]->(Component)
(Issue)-[HAS_LABEL]->(Label)
(Issue)-[HAS_STATUS]->(Status), 생성일/해결일
```

### 저장: SQLite (app_data.db) — 규모(수만 노드)에서 그래프 DB 불필요

```sql
graph_nodes(id, type, key, label, meta_json)           -- type: issue/person/component/...
graph_edges(src_id, dst_id, rel, meta_json, created_at)
CREATE INDEX idx_edges_src ON graph_edges(src_id, rel);
CREATE INDEX idx_edges_dst ON graph_edges(dst_id, rel);
```

### 질의 라우팅

query_rewriter의 분류(#52에서 is_question 추가한 지점)에 `intent` 확장:
- `fact`(기본) → 기존 파이프라인
- `aggregate`(집계·목록·"최근 ~들") → **그래프 조회 경로**: 조건(프로젝트/기간/키워드/담당자)을
  LLM이 구조화 → SQL 생성이 아닌 **사전 정의된 조회 템플릿**(파라미터 바인딩)으로 안전 실행
  → 결과 목록 + LLM 요약. (자유 SQL 생성은 금지 — 인젝션·오류 리스크)
- 그래프 결과가 빈약하면 기존 파이프라인으로 폴백

### 기대 효과 (즉시)
- "WPLUS에서 최근 한 달 쿠폰 관련 이슈" → 그래프 필터(프로젝트+기간+라벨/텍스트) → 전수 목록 + 요약
- "OOO가 담당한 이슈들" → ASSIGNED_TO 조회
- 기존 `_inject_jira_docs`/recency 필터의 상위 호환 — 해당 로직 흡수 가능

## Phase 2 — 선택적 엔티티 추출 (문서-엔티티 링크)

목적: Jira 밖(Confluence/SharePoint/Teams) 문서를 엔티티로 연결.

1. **엔티티 사전 구축**: 질문 로그 + #58 위키 토픽에서 빈발 엔티티(제품/기능/정책명) 추출
   → `entities` 테이블 (별칭 포함: "위메포"→"위메프오"). 초기 수십~수백 개, 관리자 편집 가능
2. **문서-엔티티 링크**: 로더에서 신규/변경 문서만 대상으로
   - 1차: 사전 기반 문자열/별칭 매칭 (LLM 불필요, 전 문서 커버)
   - 2차(선택): 매칭 애매한 문서만 경량 LLM 확인
   → `graph_edges(doc→entity, MENTIONS)`
3. **서빙**: 엔티티 중심 질문("X 관련 정책 전부") → 해당 엔티티 링크 문서 집합을 검색 후보에
   주입(기존 extra_queries 패턴) + 멀티홉은 엔티티-엔티티 동시출현 edge로 확장

비용: 사전 매칭이 주력이라 LLM 비용은 증분 문서의 일부에만 발생.

## Phase 3 — 커뮤니티 요약 (효과 검증 후 결정)

Phase 1~2 운영 지표(그래프 경로 사용률, 답변 피드백)가 좋을 때만:
- 엔티티 그래프 클러스터링(연결 요소/Leiden) → 클러스터별 요약 페이지 생성(#58 page_builder 재사용)
- "전체적으로 무슨 이슈가 많아?" 같은 글로벌 질문을 클러스터 요약 map-reduce로 응답
- 이 단계가 사실상 "선택적 풀 GraphRAG" — 여기서도 요약 대상은 상위 클러스터로 제한

## 모듈 구조 (예정)

```
src/company_llm_rag/graph/
├── graph_store.py      # nodes/edges CRUD + 조회 템플릿
├── jira_graph.py       # Jira JSONL → 그래프 적재 (로더 훅)
├── entity_dict.py      # 엔티티 사전 + 별칭 매칭 (P2)
├── query_router.py     # intent=aggregate 판정 → 템플릿 조회 → 요약
└── templates.py        # 사전 정의 조회 템플릿 (기간/프로젝트/담당자/키워드 바인딩)
```

## 검증 시나리오 (Phase 1 TC)

| # | 시나리오 | 기대 결과 |
|---|---|---|
| 1 | Jira 적재 후 그래프 구축 | 이슈 수 = 노드 수 일치, 이슈링크 edge 존재 |
| 2 | "WPLUS 최근 한 달 쿠폰 이슈" | aggregate 라우팅 → 전수 목록+요약 (top-K 아님) |
| 3 | "OOO 담당 이슈" | ASSIGNED_TO 조회 결과 |
| 4 | 그래프 결과 0건 | 기존 파이프라인 폴백, 답변 품질 저하 없음 |
| 5 | fact 질문 | 그래프 경로 미사용 (기존과 동일 지연) |
| 6 | 조회 템플릿 밖 요청 | 안전 폴백 (자유 SQL 생성 없음 확인) |
| 7 | 재수집 후 | 그래프 증분 갱신 (삭제 이슈 정리 포함) |

## 일정·의존성

1. **선행**: #58 Phase 1 완료 (수요 데이터 재사용), 수집 정상화 (완료 — 2026-07-10)
2. Phase 1: 그래프 적재 + aggregate 라우팅 (예상 3~4일)
3. Phase 2: 엔티티 사전 + 링크 (예상 3~4일, #58 위키 토픽 재사용)
4. Phase 3: 지표 검토 후 별도 결정

## 리스크

| 리스크 | 대응 |
|---|---|
| intent 오분류로 fact 질문이 그래프 경로行 | aggregate 판정을 보수적으로 + 폴백 보장 |
| LLM의 자유 SQL 생성 유혹 | 금지 — 사전 정의 템플릿 + 파라미터 바인딩만 |
| 그래프 낡음 (수집 실패 시) | 수집 성공 시에만 갱신 + 그래프에 기준일 표기 |
| Phase 3 과투자 | 사용률·피드백 지표 게이트 명시 |
