"""RAG 평가(#63 1단계) — 골든셋 채점 러너.

`runner.py`는 네트워크 호출이 없는 순수 함수만 담아 pytest로 검증하고,
`scripts/eval_rag.py`가 실제 `rag_query`/judge LLM을 연결하는 얇은 CLI입니다.
"""
