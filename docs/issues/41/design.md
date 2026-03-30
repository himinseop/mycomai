# Issue #41: Knowledge Hub 직접 응답 시스템

## 배경

Teams Knowledge Hub 채널에는 담당자가 질문에 회신한 검증된 답변이 있다. 동일하거나 유사한 질문이 RAG에 인입되면 LLM을 거치지 않고 이 원문 답변을 직접 반환하여 속도와 정확성을 모두 확보한다.

## 설계

### 환경변수 통합

기존에 흩어져 있던 Knowledge Hub 관련 설정을 `KNOWLEDGE_HUB_` 프리픽스로 통합.

| 기존 | 신규 | 비고 |
|------|------|------|
| `TEAMS_KNOWLEDGE_HUB_TEAMS` | `KNOWLEDGE_HUB_TEAM_NAME` | 단일 팀명 |
| `BOOST_KNOWLEDGE_HUB_RRF` | `KNOWLEDGE_HUB_RRF_BOOST` | 기본값 3.0 → 5.0 |
| `TEAMS_INQUIRY_WEBHOOK_URL` | `KNOWLEDGE_HUB_WEBHOOK_URL` | 이름 변경 |

`TEAMS_GROUP_NAME`에서 Knowledge Hub를 제거하고, `KNOWLEDGE_HUB_TEAM_NAME`으로 독립 수집.

### 데이터 수집 파이프라인

```
Teams Knowledge Hub 채널 (Adaptive Card 메시지 + Reply)
  ↓
[teams_extractor.py]
  1. reply가 있는 메시지만 수집 (미답변 질문 제외)
  2. Adaptive Card에서 [질문] 섹션 텍스트 추출
  3. Reply HTML에서 답변 원문 추출
     - <img> 태그 → Graph API로 이미지 다운로드 → static/images/에 저장
     - 이미지 위치에 마크다운 ![참고 이미지](/static/images/xxx.png) 삽입
  4. JSONL 출력: content=질문, metadata.hub_reply_content=답변원문, metadata.is_hub_direct=True
  ↓
[data_loader.py]
  1. 중복 질문 감지 (hub_find_duplicate)
     - 동일 질문 존재 시: 기존 임베딩 재활용, SQLite 답변 포인터만 변경
     - 새 질문: ChromaDB에 질문 임베딩, SQLite에 답변 저장
  2. ChromaDB: 질문 텍스트만 임베딩 (is_hub_direct=True 메타데이터)
  3. SQLite hub_replies: 답변 원문 저장 (is_active=1)
     - 기존 답변은 is_active=0으로 비활성화 (이력 보관)
```

### 검색 및 응답

```
[사용자 질문 인입]
  ↓
[retrieval_module.py] 벡터 + FTS5 하이브리드 검색
  - Knowledge Hub 문서에 RRF 5.0x 부스트 적용
  ↓
[rag_system.py] _try_hub_direct_answer()
  - 1위가 Hub 문서 (is_hub_direct=True)?
  - 1위 RRF ÷ 2위 RRF ≥ 2.0 (충분히 우세)?
    → Yes: SQLite hub_replies에서 답변 원문 조회
           gpt-4o-mini로 안내 멘트 생성 (질문 내용 풀어서 설명)
           안내 멘트 + 구분선 + 답변 원문 반환 (참고문서 비표시)
    → No:  기존 LLM RAG 파이프라인
```

### 응답 예시

```
이 질문은 고객이 현금영수증 발행을 위해 휴대폰번호를 기재하지 않아
발급이 어려운 경우에 대한 해결 방법에 관한 내용입니다.
관련하여 유사한 질문의 답변을 안내드립니다.

---

(Knowledge Hub 답변 원문 — 이미지 포함, 수정 없이 그대로)
```

### 중복 질문 처리

- 동일한 질문 텍스트가 여러 메시지로 인입되는 경우:
  - 기존 ChromaDB 임베딩 재활용 (새 임베딩 생성하지 않음)
  - SQLite에 새 답변을 추가하고 기존 답변을 비활성화
  - `hub_get_reply_history(doc_id)`로 답변 변경 이력 조회 가능

### DB 스키마

#### hub_replies (app_data.db)

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | INTEGER | PK, 자동 증가 |
| `doc_id` | TEXT | 원본 Teams 메시지 ID |
| `question` | TEXT | 질문 텍스트 |
| `reply_content` | TEXT | 답변 원문 (마크다운 이미지 포함) |
| `created_at` | TEXT | 답변 저장 시각 (ISO8601) |
| `is_active` | INTEGER | 활성 답변 여부 (1=현재, 0=이전 버전) |

### 관련 파일

| 파일 | 변경 내용 |
|------|----------|
| `config.py` | KNOWLEDGE_HUB_* 환경변수 3개 추가, 기존 설정 제거 |
| `teams_extractor.py` | Adaptive Card 파싱, 이미지 다운로드, 질문/답변 분리 |
| `data_loader.py` | Hub 문서 질문만 임베딩, 답변 SQLite 저장, 중복 감지 |
| `history_store.py` | hub_replies 테이블, hub_upsert/get_reply/find_duplicate/get_reply_history |
| `retrieval_module.py` | KNOWLEDGE_HUB_TEAM_NAME 기반 RRF 5.0x 부스트 |
| `rag_system.py` | _try_hub_direct_answer, _build_hub_intro (안내 멘트 LLM 생성) |
| `teams_sender.py` | KNOWLEDGE_HUB_WEBHOOK_URL로 변수명 변경 |
| `sharepoint_extractor.py` | Knowledge Hub 팀 SharePoint도 자동 수집 |
| `templates/index.html` | 이미지 렌더링 CSS (.qa-a-text img) |
| `static/images/` | Teams 답변 이미지 로컬 저장소 |
