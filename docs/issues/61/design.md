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

## 11-A. 다이제스트 수집 (2026-08-29 확정 — 이슈 코멘트의 결정 사항 구체화)

docs 저장소 `platform/sharepoint-index/digests/*.md` (606건, 파일명 `YYYY-MM_제목.md`)를
수집한다. **다이제스트가 원본 SharePoint 청크를 대체**하는 것이 핵심 원칙.

### 헤더 계약 (파일럿 검증 완료 — voucher)
blockquote 헤더 블록에서 파싱:
```
> **이 문서는 YYYY년 M월 시점의 기획이다. …**       ← 시점 경고 (본문에 유지)
> - 원본: [파일명](SharePoint URL)                  ← sourcedoc={GUID} 추출 (경로형 URL이면 GUID 없음)
> - 문서 기록 날짜: YYYY-MM-DD · 종류: 기획서 · 버전: ver0.4   ← 날짜·종류·버전
> - SharePoint 위치: `/...`
> - 관련 주제: [voucher](../../features/voucher.md), ... ← 마크다운 링크 또는 평문 슬러그 둘 다 처리
> - 관련 일감: 구현 WMPO-123(사유) · 후속 WPLUS-45(사유) · 원인 KEY · 미구현(비고)
```
- 관련 일감 역할 어휘 4종 고정: 구현/후속/원인/미구현. `> - 관련 일감:` 줄만 인식
  (본문 산문에 같은 문자열 존재 — 앵커 필수). 4종 외 어휘는 무시+경고 로그.
- 관련 주제 슬러그: `voucher` → `features/voucher.md`, `site:admin`·`admin` 링크 → `sites/admin.md`

### 수집·메타데이터
- `DOCS_REPO_SUBDIRS` 기본값에 `platform/sharepoint-index/digests` 추가
  (master에 디렉토리가 없으면 기존 경고 후 스킵 — 머지 전 야간 수집 안전)
- content: 헤더의 URL 포함 줄(원본·위치)은 제거, 시점 경고 문장은 유지 (헤지 보존 규칙과 연동)
- metadata: `docs_category='digest'`, `digest_date`(기록 날짜), `digest_kind`(종류),
  `digest_version`, `sp_guid`(대문자, 없으면 ''), `digest_topics`(콤마 슬러그),
  `digest_issues`(JSON 문자열: [{"role":"구현","key":"WMPO-123","note":"사유"}...]),
  `not_implemented`(bool)
- url: 원본 SharePoint URL (참고문서 노출용 — 매뉴얼과 달리 다이제스트는 원본 링크 노출)

### 검색 반영
- **부스트 분리**: `docs_category=='digest'`는 DOCS_RRF_BOOST·DOCS_RERANK_BOOST 제외,
  `_source_boost`는 BOOST_DIGEST(기본 0.85) 적용. `not_implemented`는 추가 감쇠(×1.2 distance)
- **원본 대체**: data_loader가 다이제스트의 sp_guid 집합을 app_data.db `digest_guids`
  테이블에 저장 → retrieval에서 sharepoint 문서 url의 GUID가 집합에 있으면 distance ×1.3
  (제거 아님 — 원문 상세 질의는 여전히 가능), 캐시 5분
- **dedup 대표 교체** (E2E에서 발견·수정): 다이제스트의 url이 원본과 동일해 문서 단위
  중복 제거에서 같은 키로 충돌 — 리랭커가 원본을 위로 올리면 다이제스트가 '중복'으로
  탈락하는 역전이 발생했다. 같은 URL 충돌 시 다이제스트가 그 순위 자리를 대표하도록
  교체 (원본 청크가 이긴 자리에 다이제스트 삽입, 원본은 제거)
- **역할 라벨**: `[기획 다이제스트 | {종류} | {YYYY-MM} — 당시 기획]`,
  미구현이면 `— 미구현 기획` 추가. 참고문서에는 원본 SharePoint 링크로 노출
  (references 빌더의 docs 제외 규칙에서 digest는 예외)
- 생성 규칙 추가: 다이제스트는 당시 기획 — 현행 정책 질문에는 매뉴얼 우선,
  배경·이력·세부 스펙 인용으로만 사용. 미구현 기획은 반드시 그 사실을 밝힘
- **그래프**: doc 노드(`doc:docs-<relpath>`) + MENTIONS 엣지
  — digest→entity (topics, method='digest', score 1.0)
  — digest→issue (digest_issues, meta {"method":"digest","role":...,"note":...})

### TC
| # | 시나리오 | 기대 |
|---|---|---|
| D1 | 브랜치 오버라이드 수집 (INFRA-40 체크아웃) | 606건 파싱, 헤더 필드 누락 시 경고만 (수집은 계속) |
| D2 | 관련 주제 두 형식 (평문/마크다운) | 동일 슬러그로 정규화 |
| D3 | 관련 일감 파싱 | 구현14·후속1·미구현2 (2026-08-29 기준) + 본문 산문 오탐 0 |
| D4 | 부스트 분리 | digest가 매뉴얼 부스트를 받지 않음 (동일 질의에서 매뉴얼이 digest 위) |
| D5 | 원본 다운랭크 | GUID 매칭 sharepoint 원본이 digest 아래로 |
| D6 | 참고문서 | digest는 원본 SharePoint 링크로 노출, 매뉴얼은 계속 비노출 |
| D7 | 미구현 | 답변에 미구현 사실 명시 + 다운랭크 |
| D8 | 회귀 | 현행 정책 질문(매뉴얼)·Hub 직접응답·집계 경로 불변 |

## 11. 리스크

| 리스크 | 대응 |
|---|---|
| L2 과잉 링크 (일반 용어 엔티티: 회원·알림·포인트) | 거리 임계치 보수적(0.45) + 엔티티당 상한 N + P2 block |
| 오래된 SharePoint 스펙이 현행 정책과 상충 | 라벨에 날짜 + "과거 기획" 규칙, 버전 시리즈 최신만 |
| 역할 라벨로 프롬프트 길이 증가 | 라벨은 한 줄, 기존 헤더 대체 |
| 재구축 시 임베딩 비용 | 엔티티당 1회 쿼리 — 26회/야간 |

## 12. 출처 기반 참고문서 (2026-09-02 확정)

문제: 참고문서가 검색 결과(키워드 유사성) 그대로라, 매뉴얼 근거 답변에서 지라/컨플/
쉐어포인트가 무관하게 딸려온다.

방향: 매뉴얼 저자가 해당 구문 옆에 지정한 출처(인라인 다이제스트 링크·이슈키)를
참고문서로 제공한다. 없으면 비운다.

### 모드 결정
- **매뉴얼 근거 모드**: LLM 컨텍스트에 포함된 문서 중 최상위가 매뉴얼(source=docs,
  docs_category≠digest)일 때. Hub 직접응답·집계·폴백 RAG는 기존 로직 유지.

### 매뉴얼 근거 모드의 참고문서 구성 (우선순위 순)
1. 컨텍스트에 포함된 매뉴얼 청크 본문에서 다이제스트 링크 추출
   (`../sharepoint-index/digests/X.md` — 매뉴얼 relpath 기준 normpath 정규화)
   → 그래프 doc 노드에서 원본 SharePoint URL 조회 → 기존 digest ref 형식으로 노출
2. 그 다이제스트의 관련 일감(digest_issues) — 구현 역할 우선, 다이제스트당 최대 2
3. 매뉴얼 청크에 직접 인용된 지라 이슈키 — **그래프에 실존하는 이슈만** (기능 ID
   `ECP-C-01` 류 오탐 방지)
4. LLM이 [REF]로 실제 인용한 문서 + injected/hub 문서는 유지
5. 그 외 검색 결과(키워드로만 걸린 지라/컨플/쉐어포인트)는 제외. 0건이면 빈 목록.

- 전부 app_data.db 그래프 조회 — LLM 추가 호출·지연 없음
- URL·이슈키 dedup, 전체 상한은 기존 _MAX_REFERENCES 준수

### 구현 노트 (2026-09-02 구현 시 확인한 사실)
- 다이제스트 doc 노드 id는 `doc:{doc_id}` (`doc_id`=`docs-{repo-root-relpath}`,
  예: `doc:docs-platform/sharepoint-index/digests/2023-02_voucher.md`) —
  `docs_relpath`는 항상 `platform/` 접두사 포함 저장소 루트 기준 경로
  (`config.py: DOCS_REPO_SUBDIRS` 기본값이 `platform/...`로 시작, `docs_extractor.build_document()`).
  이슈 노드 id는 `issue:{KEY}`(`jira_graph.py`).
- `graph_store.py`에 파라미터 바인딩 배치 조회 `get_nodes(node_ids) -> Dict[id, {id,type,label,meta}]`를
  신규 추가 — provenance 모듈이 다이제스트 doc 노드·이슈 노드 존재 검증에 공용으로 사용.
- 매뉴얼 본문의 다이제스트 인라인 링크(`관련 기획: [...](../sharepoint-index/digests/X.md)`)는
  이 저장소에는 실제 예시가 없다(docs_repo는 별도 마운트 저장소이며 매뉴얼 저자가 앞으로
  추가할 링크). `extract_from_chunks`는 특정 문구가 아니라 **마크다운 링크 href에
  `sharepoint-index/digests/`가 포함되는지**로 일반화 구현했고, 매뉴얼의 `docs_relpath`
  기준 `posixpath.normpath(dirname(docs_relpath) + href)`로 정규화한다.
  href의 URL 인코딩·타이포그래피 문자는 디코딩하지 않고 그대로 보존(문자열 경로 연산만 수행).
- `rag_system._build_references`에 `only_priority`/`max_refs_override` 파라미터를 추가해
  기존 로직(인용/injected/Hub 문서 판별, distance 필터, teams URL, hub_reply 조회 등)을
  그대로 재사용 — 매뉴얼 근거 모드에서 "인용·injected·Hub 문서만" 뽑을 때 로직 중복 없이 호출.
- 모드 판정(`_is_manual_grounded`)은 `retrieved_docs[0]`(build_rag_prompt에 그대로 전달되는
  최종 리스트의 첫 항목)을 "컨텍스트 최상위 1순위"로 본다 — `rag_query`/`rag_query_stream`
  모두 `_inject_jira_docs`/`inject_entity_docs` 이후 별도 top-K 절단이 없어 이 리스트가
  곧 LLM 프롬프트 컨텍스트와 동일하다(확인 완료).
- 구현 파일: `src/company_llm_rag/rag/provenance.py`(신규), `src/company_llm_rag/graph/graph_store.py`
  (`get_nodes` 추가), `src/company_llm_rag/rag_system.py`(`_is_manual_grounded`,
  `_build_manual_grounded_references`, `_build_references` 파라미터 확장), 테스트
  `tests/test_provenance_refs.py`(신규).

### §12 검증 후 확정 (2026-09-02 E2E)
- **주입(entity link) 문서는 참고문서에서 제외** — 주제 수준 연결이라 구문 출처가 아님.
  E2E에서 무관한 지라/컨플이 그대로 재유입되는 것 확인 후 결정 (`include_injected=False`)
- **컨텍스트 다이제스트 유지** — 부스트·리랭크를 통과해 컨텍스트에 든 다이제스트는 답변의
  정책 근거. relpath를 provenance에 합류시켜 원본 링크 + 관련 일감까지 노출
- **모드 확장**: 다이제스트 최상위도 출처 기반 모드 (다이제스트 근거 답변도 잡음 제외)
- **추출 범위 축소**: 1순위 문서와 같은 매뉴얼(docs_relpath)의 청크에서만 링크·이슈키 추출
  — 컨텍스트 하위의 다른 주제 매뉴얼 청크에서 긁으면 무관 링크 유입 (E2E 확인)
- **1순위 판정**: inject_entity_docs가 주입 문서를 리스트 앞에 붙이므로 `_injected` 제외
  첫 문서를 검색 1순위로 판정 (`_top_retrieved_doc`)
- 잔존(범위 밖): 비-docs 근거 답변(지라 목록형 등)의 기존 경로에는 주입 문서가 참고문서
  상단에 남음 — 재작성문 기반 엔티티 감지의 보수화와 함께 후속 튜닝 후보
