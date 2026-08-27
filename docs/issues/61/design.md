# Issue #61 플랫폼매뉴얼 기준 검색 플로우 — 문서 관계 모델·링크 거버넌스

GitHub Issue: https://github.com/himinseop/mycomai/issues/61
선행: #59 GraphRAG (Phase 1 Jira 그래프, Phase 2 엔티티 링크 — 본 이슈는 P2의 확장)

## 1. 배경과 문제

sess-4m6keh7 분석 결과, 할루시네이션은 두 단계로 발생한다.

1. **검색**: Jira/Confluence의 "특정 시점 이슈·회의록"이 표면적 유사도로 상위에 올라옴
   (로그인 얼럿 이슈가 쿠폰 난수번호 질문에 매칭)
2. **생성**: LLM이 개별 이슈 사례를 **현행 정책처럼 일반화**

현재 엔티티 링크(#59 P2)는 제목 별칭 매칭만 사용해 **전체 이슈의 22%(4,698/21,054)**만
매뉴얼에 연결되어 있고, 연결에 "역할"이 없어 컨텍스트에서 매뉴얼과 이슈가 동급으로 취급된다.

## 2. 설계 원칙

- **매뉴얼 = 기준(anchor)**, 나머지 소스 = 매뉴얼 주제에 연결된 **역할 있는 근거(evidence)**
- 링크는 **제한이 아니라 부스트** — 비링크 78% 영역(매뉴얼 밖 운영·인프라)은 기존 플로우로 폴백
- LLM은 링크를 만들지 않는다 — 문자열·임베딩·상호참조 기반, 비용 0에 가깝게
- 관리자 튜닝은 자동 링크에 덮어써지지 않는다 (pin/block 거버넌스)

## 3. 문서 역할 모델

| 소스 | role | 답변에서의 쓰임 | 신뢰 서열 |
|---|---|---|---|
| docs (플랫폼매뉴얼) | `policy` 현행 정책 기준 | 일반론·정책 답변의 유일한 근거 | 1 |
| Knowledge Hub 답변 | `faq` 검수 FAQ | 담당자 확정 답 | 2 |
| confluence | `reference` 참고문서 | 프로세스 상세·정의 보강 | 3 |
| sharepoint | `spec` 세부 스펙 | 정책의 원천 설계 — **시점 주의** | 4 |
| jira (Bug/Support/운영) | `case` 사례 | "~사례가 있었다(이슈키·시점)" 인용 전용 | 5 |
| jira (Epic/착수) | `origin` 변경 기원 | "이 정책은 ~에서 도입" 배경 | 5 |
| teams | `discussion` 현장 문답 | 최신 실무 맥락, 단정 금지 | 6 |

role은 **서빙 시점에 문서 메타데이터에서 유도**한다 (엣지에 저장하지 않음):
`source` + `jira_issue_type`(Epic/Story → origin, 그 외 → case). 엣지 메타에는 `method`·`score`만 저장.

시점 규칙: 컨텍스트 라벨에 날짜 포함. 매뉴얼과 상충하는 오래된 스펙은 "과거 기획"으로만 언급.

## 4. 3단 링크 빌더 (적재 시 연결)

기존 `graph_edges(src=문서노드, dst=entity, rel='MENTIONS')`를 그대로 쓰고
`meta_json`에 `{"method": "title|embed|key", "score": float}`를 기록한다.

| 단계 | 방법 | score | 비고 |
|---|---|---|---|
| L1 (현행) | 엔티티 이름·별칭 ↔ 노드 label(제목) LIKE | 0.7 | 정밀도 최상 |
| L2 (신규) | 엔티티 이름 + 매뉴얼 제목 + 매뉴얼 첫 청크(개요)를 쿼리 텍스트로 ChromaDB `query` (where source ∈ {jira, confluence}), 거리 임계치 이하 상위 N | `1 - distance` | 용어가 달라도 연결 |
| L3 (신규) | 매뉴얼 청크 본문에서 `[A-Z]+-\d+` 이슈키 추출 → 해당 issue 노드 링크 | 1.0 | 담당자 검수 링크, 최고 신뢰 |

같은 (src, dst) 쌍이 여러 방법으로 잡히면 **score가 높은 쪽으로 갱신** (INSERT OR REPLACE 시 max 비교).

매뉴얼 청크는 ChromaDB에 이미 적재되어 있으므로 `collection.get(where={"docs_relpath": relpath})`로
읽는다 — docs_repo 마운트가 없는 web 컨테이너(관리자 재구축)에서도 동작해야 한다.

설정 (config.py):
```python
MANUAL_LINK_EMBED_TOP_N: int = 50        # 매뉴얼당 L2 후보 상한
MANUAL_LINK_EMBED_MAX_DIST: float = 0.45 # L2 거리 임계치 (초과 시 링크 안 함)
```

## 5. 링크 거버넌스 (P2)

```sql
link_overrides(
  id INTEGER PK, entity_name TEXT, target_node_id TEXT,  -- issue:KEY / doc:confluence-xxx
  action TEXT CHECK(action IN ('pin','block')),
  created_by TEXT, created_at TEXT, UNIQUE(entity_name, target_node_id)
)
```
재구축 마지막 단계에서 적용: block → 해당 엣지 삭제, pin → 엣지 보장(score=1.0, method='pin').
서빙 주입 순서: pin > key > title > embed, 동률이면 최신순.

## 6. 서빙 — 역할 라벨 컨텍스트 + 생성 규칙 (P1)

### 6.1 컨텍스트 라벨 (`rag/citations.py: doc_source_label`)
문서 헤더에 역할을 명시한다. 예:
```
[플랫폼매뉴얼 | 현행 정책 기준] 제목: 쿠폰 가이드 | 분류: features
[Jira | 개별 사례 — 정책 일반화 금지] 제목: ... | 유형: Bug | 상태: Closed | 담당자: ... | 날짜: 2024-10-21 | URL: ...
[Jira | 변경 기원] ... (Epic/Story)
[Confluence | 참고문서] ...
[SharePoint | 세부 스펙 — 시점 주의] ... | 날짜: ...
[Teams | 현장 문답 — 단정 금지] ...
```

### 6.2 생성 규칙 (`prompts/rag_instructions.txt`에 추가)
- 정책·규격의 일반론은 `현행 정책 기준` 라벨 문서에서만 답한다
- `개별 사례` 문서는 "~한 사례가 있었습니다(이슈키, 시점)"로만 인용하고 정책으로 일반화하지 않는다
- 매뉴얼과 사례/스펙이 상충하면 매뉴얼을 우선하되 상충 사실을 한 문장으로 밝힌다
- 매뉴얼에 없고 사례만 있으면 "정책 문서에는 없으나, 관련 사례로는 ~" 구조로 답한다

### 6.3 주입 우선순위 (`graph/entity_link.py: _mentioned_nodes`)
현재 최신순만 → `method` 우선순위(key > title > embed) 후 최신순으로 변경.

## 7. 2단계 검색 플로우 (P2)

```
질문 → 해석·재작성 (기존)
  1단계: 매뉴얼 매칭 — 엔티티 감지(기존) + source=docs 한정 벡터 상위 → 주제 매뉴얼 0~2개
  2단계: A 주제 매뉴얼 청크(질문과 리랭크 상위)
         B 링크 문서 중 질문과 리랭크 상위 N
         C 일반 하이브리드 결과 — 링크 문서 가점 / 비링크 감점 (제거 안 함)
  컨텍스트 조립: 6.1 역할 라벨
```
매뉴얼 매칭 실패 시 기존 플로우 그대로.

## 8. 소스별 매칭·튜닝 전략 (P2~P4)

| 소스 | 매칭 | 튜닝 필요도 | 단계 |
|---|---|---|---|
| Jira | L1 + L2 + L3, 이슈타입 role 분기 | 낮음 | P1 |
| Confluence | L1 + L2 | 낮음 | P1 |
| SharePoint | 파일 단위 L2 + 파일명 별칭, 버전 시리즈(파일명 prefix 동일) 최신 1개만 | **높음** | P2 |
| Hub 답변 | 질문 텍스트 ↔ 별칭 + 임베딩. 원천 개선: Teams 카드 `[주제]` 필드 태깅 | 낮음 | P3 |
| Teams | L2만, 보수적 임계치 | **높음** | P4 |

튜닝 진입점: ① 매뉴얼별 연관 문서 탭(score 낮은 순, pin/block) ② 질문 상세 "이 주제 아님" 버튼
③ 👎 피드백 답변의 주입 문서 자동 검토 큐.

## 9. 구현 계획

### P1 — 링크 강화 + 역할 라벨 (본 이슈 1차 범위)
접점 파일:
- `config.py`: `MANUAL_LINK_EMBED_TOP_N`, `MANUAL_LINK_EMBED_MAX_DIST`
- `graph/entity_link.py`:
  - `rebuild_entities()`: L1 엣지에 meta `{"method":"title","score":0.7}` 기록,
    L2 `_link_by_embedding(entity)`, L3 `_link_by_issue_keys(entity)` 추가. score max 갱신.
    반환 stats에 method별 건수 포함 (`{"title": n, "embed": n, "key": n}`)
  - `_mentioned_nodes()`: method 우선순위 정렬 (key > title > embed) → 최신순
- `rag/citations.py: doc_source_label()`: 6.1 역할 라벨
- `prompts/rag_instructions.txt`: 6.2 규칙
- `rag_system.py`: 변경 없음 (주입 경로 그대로)
- 관리자 "링크 재구축" 응답에 method별 건수 노출 (`admin.html` 상태 문구)

제약:
- L2는 ChromaDB `query`를 쓰므로 OpenAI 임베딩 호출 발생 (엔티티당 1회, 26회) — 재구축 시에만
- web 컨테이너에는 docs_repo 마운트가 없다 → 매뉴얼 본문은 반드시 ChromaDB에서 읽는다
- 자유 SQL 금지 원칙 유지 (파라미터 바인딩)
- 기존 동작(집계 라우팅, Hub 직접응답, 위키)에 영향 없어야 함

### P2 — 거버넌스 + SharePoint + 관리자 탭
### P3 — Hub 매칭 + 피드백 큐
### P4 — Teams 매칭

## 10. 검증 시나리오 (P1 TC)

| # | 시나리오 | 기대 결과 |
|---|---|---|
| 1 | 링크 재구축 (web 컨테이너, docs_repo 미마운트) | 오류 없이 완료, stats에 title/embed/key 건수 |
| 2 | 링크 커버리지 | MENTIONS 있는 고유 이슈 수가 4,698보다 유의미하게 증가 |
| 3 | L3 | 매뉴얼 본문에 이슈키가 있으면 해당 issue 노드에 method=key 엣지 |
| 4 | L2 임계치 | distance > MAX_DIST 문서는 링크되지 않음 |
| 5 | 주입 우선순위 | 같은 엔티티에서 key/title 링크가 embed보다 먼저 주입 |
| 6 | 역할 라벨 | LLM 프롬프트의 문서 헤더에 `[플랫폼매뉴얼 \| 현행 정책 기준]`, `[Jira \| 개별 사례 …]` 표기 |
| 7 | 생성 규칙 | "쿠폰 난수번호 대소문자" 질문 → 문서에 없으면 확인 불가, 사례 인용 시 이슈키·시점 명시, 정책 일반화 없음 |
| 8 | 회귀 | 집계 질문("WPLUS 최근 한 달 쿠폰 이슈") 그래프 경로 정상, 잡담 응대·Hub 직접응답 정상 |
| 9 | 재구축 멱등성 | 두 번 재구축해도 엣지 수 동일 (중복 없음) |

## 11. 리스크

| 리스크 | 대응 |
|---|---|
| L2 과잉 링크 (일반 용어 엔티티: 회원·알림·포인트) | 거리 임계치 보수적(0.45) + 엔티티당 상한 N + P2 block |
| 오래된 SharePoint 스펙이 현행 정책과 상충 | 라벨에 날짜 + "과거 기획" 규칙, 버전 시리즈 최신만 |
| 역할 라벨로 프롬프트 길이 증가 | 라벨은 한 줄, 기존 헤더 대체 |
| 재구축 시 임베딩 비용 | 엔티티당 1회 쿼리 — 26회/야간 |
