"""
리랭커 A/B: bge-reranker-v2-m3(fp16) vs bge-reranker-base(fp32)

실제 사용자 질문(chat_history)으로 리랭커 없이 후보를 라이브 검색한 뒤,
두 모델의 랭킹을 LLM-judge(gpt-4o-mini, 블라인드 0~2점)로 NDCG@3 비교합니다.
후보 수집(ChromaDB) → 해제 → 모델 순차 로드로 메모리 피크 최소화.

사용(web 중지 상태 권장 — 메모리 여유):
  docker-compose run --rm --no-deps -v $PWD/scripts:/app/scripts base \
    python3 /app/scripts/ab_reranker.py
"""
import gc
import math
import time

from company_llm_rag.config import settings
from company_llm_rag.history_store import _conn
from company_llm_rag.llm.factory import summarizer_llm

MODELS = [
    ("BAAI/bge-reranker-v2-m3", "float16"),
    ("BAAI/bge-reranker-base", "float32"),
]
N_QUERIES = 10
TOP_K = 3
DOC_CHARS = 256


def load_questions():
    """실사용 질문 로드 (중복·URL·짧은 입력 제외)."""
    with _conn() as con:
        rows = con.execute(
            "SELECT DISTINCT question FROM chat_history "
            "WHERE is_no_answer=0 AND length(question) > 8 "
            "ORDER BY id DESC LIMIT 60"
        ).fetchall()
    qs = []
    for r in rows:
        q = r["question"].strip()
        if q.startswith("http") or "sess-" in q or q in qs:
            continue
        qs.append(q)
        if len(qs) >= N_QUERIES:
            break
    return qs


def collect_candidates(questions):
    """리랭커 없이 하이브리드 검색으로 후보 수집 (수집 후 ChromaDB 해제)."""
    settings.RERANKER_ENABLED = False  # 후보는 RRF 순수 결과로
    from company_llm_rag.retrieval_module import retrieve_documents
    cases = []
    for q in questions:
        docs = retrieve_documents(q, n_results=10)
        docs = [d for d in docs if (d.get("content") or "").strip()]
        if len(docs) < 5:
            print(f"  후보 부족으로 제외: {q[:40]}")
            continue
        cases.append({
            "q": q,
            "docs": [{
                "title": (d.get("metadata") or {}).get("title", ""),
                "content": (d.get("content") or "")[:DOC_CHARS],
            } for d in docs[:10]],
        })
    # ChromaDB 참조 해제 (이후 단계는 리랭커 모델만 필요)
    import company_llm_rag.database as database
    database.db_manager._collection = None
    database.db_manager._client = None
    gc.collect()
    return cases


def rank_with(model_name, dtype, cases):
    """모델 하나로 모든 케이스 랭킹 + 케이스당 평균 지연 측정."""
    import torch
    from sentence_transformers import CrossEncoder
    kwargs = {"torch_dtype": torch.float16} if dtype == "float16" else {}
    m = CrossEncoder(model_name, max_length=256, model_kwargs=kwargs)
    out, times = [], []
    for c in cases:
        pairs = [(c["q"], f"{d['title']} {d['content']}") for d in c["docs"]]
        t0 = time.monotonic()
        scores = m.predict(pairs)
        times.append(time.monotonic() - t0)
        order = sorted(range(len(scores)), key=lambda i: -scores[i])
        out.append(order)
    del m
    gc.collect()
    return out, sum(times) / len(times)


def judge(q, doc):
    """LLM 블라인드 채점: 질문-문서 관련성 0(무관)/1(부분)/2(직접 답)."""
    prompt = (
        f"질문: {q}\n문서: {doc['title']}\n{doc['content']}\n\n"
        "이 문서가 질문에 답하는 데 얼마나 유용한지 0/1/2로만 답하세요.\n"
        "0=무관, 1=부분적 관련, 2=직접적으로 유용"
    )
    try:
        r = summarizer_llm.chat([{"role": "user", "content": prompt}],
                                model="gpt-4o-mini", temperature=0.0, max_tokens=4)
        for ch in r.strip():
            if ch in "012":
                return int(ch)
    except Exception as e:
        print(f"  judge 실패: {e}")
    return None


def ndcg_at_k(rels, k=TOP_K):
    dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rels[:k]))
    ideal = sorted(rels, reverse=True)
    idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal[:k]))
    return dcg / idcg if idcg > 0 else None


def main():
    questions = load_questions()
    print(f"평가 질문 {len(questions)}건 후보 수집 중...")
    cases = collect_candidates(questions)
    print(f"유효 케이스 {len(cases)}건")
    for c in cases:
        print(f"  - {c['q'][:60]}")

    rankings, latency = {}, {}
    for name, dtype in MODELS:
        print(f"\n[{name} ({dtype})] 랭킹 중...")
        rankings[name], latency[name] = rank_with(name, dtype, cases)
        print(f"  케이스당 평균 {latency[name]*1000:.0f}ms (후보 ~10건 기준)")

    # 채점 대상: 두 모델 top-3 합집합 (블라인드 — 어느 모델 출신인지 미노출)
    print("\nLLM 채점 중...")
    rel_cache = {}
    for ci, c in enumerate(cases):
        need = set()
        for name, _ in MODELS:
            need.update(rankings[name][ci][:TOP_K])
        for di in need:
            rel_cache[(ci, di)] = judge(c["q"], c["docs"][di])

    print("\n=== 결과 (NDCG@3) ===")
    summary = {name: [] for name, _ in MODELS}
    for ci, c in enumerate(cases):
        # 모든 모델 점수를 먼저 계산 — 하나라도 판정 불가면 케이스 전체 제외 (집계 정합)
        case_scores = {}
        for name, _ in MODELS:
            rels = [rel_cache.get((ci, di)) for di in rankings[name][ci][:TOP_K]]
            score = ndcg_at_k(rels) if None not in rels else None
            case_scores[name] = score
        if any(v is None for v in case_scores.values()):
            print(f"{c['q'][:40]:40}  (판정 불가 — 관련 문서 없음/채점 실패)")
            continue
        line = f"{c['q'][:40]:40}"
        for name, _ in MODELS:
            summary[name].append(case_scores[name])
            line += f"  {name.split('/')[-1]}={case_scores[name]:.3f}"
        print(line)
    print("\n--- 요약 ---")
    for name, _ in MODELS:
        s = summary[name]
        if s:
            print(f"{name}: NDCG@3 평균 {sum(s)/len(s):.3f} (유효 {len(s)}건) | 지연 {latency[name]*1000:.0f}ms/케이스")


if __name__ == "__main__":
    main()
