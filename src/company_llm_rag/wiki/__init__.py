"""
LLM 위키 (#58)

질문 로그에서 수요 상위 토픽을 뽑아(LLM 클러스터링), 토픽별 정리 페이지를
LLM으로 합성(출처 인용 강제)하고, 관리자 검수를 거쳐 서빙합니다.

- topic_miner: 질문 로그 → 토픽 후보
- page_builder: 토픽 → 문서 수집 → 페이지 합성(+팩트 블록)
- wiki_store:   wiki_pages CRUD + 대표 질문 임베딩 관리

서빙(Phase 1): draft/approved 모두 검색 컨텍스트 부스트(WIKI_RRF_BOOST).
직접 응답(approved 전용)은 Phase 2.
"""
