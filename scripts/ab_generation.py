"""
생성 LLM 품질 A/B: 현행 OpenAI(chat 모델) vs Ollama 후보 모델

실사용 질문으로 동일한 RAG 컨텍스트를 만들고, 두 모델의 답변을
LLM-judge(gpt-4o, 블라인드·순서 랜덤)로 채점합니다.
평가 기준: 근거 충실성(할루시네이션), 완전성, 한국어 자연스러움 → 1~5점 + 할루 여부.

사용: docker-compose run --rm --no-deps -v $PWD/scripts:/app/scripts base \
        python3 /app/scripts/ab_generation.py
"""
import json
import random
import time

from company_llm_rag.config import settings

settings.RERANKER_ENABLED = False  # 후보 검색은 RRF만 (메모리 절약, 양쪽 동일 컨텍스트)

from company_llm_rag.history_store import _conn                      # noqa: E402
from company_llm_rag.llm.openai_provider import OpenAIProvider       # noqa: E402
from company_llm_rag.rag_system import build_rag_prompt, _load_prompt  # noqa: E402
from company_llm_rag.retrieval_module import retrieve_documents      # noqa: E402

OLLAMA_CANDIDATE = "qwen2.5:7b-instruct"
JUDGE_MODEL = "gpt-4o"
N_QUERIES = 8

random.seed(42)  # 순서 랜덤화 재현성

openai_llm = OpenAIProvider()  # 현행 chat 모델 (gpt-4o-mini)
ollama_llm = OpenAIProvider(api_key="ollama", base_url=settings.OLLAMA_BASE_URL,
                            default_model=OLLAMA_CANDIDATE)
judge_llm = OpenAIProvider(default_model=JUDGE_MODEL, default_temperature=0.0)


def load_questions():
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


def generate(llm, system_prompt, prompt, model=None):
    t0 = time.monotonic()
    ans = llm.chat(
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": prompt}],
        model=model, temperature=0.3, max_tokens=800,
    )
    return ans, time.monotonic() - t0


def judge(question, docs_text, ans_a, ans_b):
    """블라인드 채점 — a/b는 호출측에서 랜덤 배치."""
    prompt = f"""당신은 사내 RAG 시스템의 답변 품질 평가자입니다.
질문과 참고 문서, 두 답변(A/B)이 주어집니다. 각 답변을 평가하세요.

[질문] {question}

[참고 문서 발췌]
{docs_text}

[답변 A]
{ans_a}

[답변 B]
{ans_b}

기준:
- score: 1~5 (근거 충실성 50% + 완전성 30% + 한국어 자연스러움 20%)
- hallucination: 참고 문서에 없는 사실·수치를 지어냈으면 true

JSON만 출력: {{"a": {{"score": n, "hallucination": bool}}, "b": {{"score": n, "hallucination": bool}}, "better": "a|b|tie"}}"""
    raw = judge_llm.chat([{"role": "user", "content": prompt}], max_tokens=150)
    s, e = raw.find("{"), raw.rfind("}")
    return json.loads(raw[s:e + 1])


def main():
    questions = load_questions()
    system_prompt = _load_prompt(settings.SYSTEM_PROMPT_FILE, "system_prompt.txt")
    print(f"평가 질문 {len(questions)}건 | 후보: {OLLAMA_CANDIDATE} vs 현행 {settings.OPENAI_CHAT_MODEL}\n")

    results = []
    for q in questions:
        docs = retrieve_documents(q, n_results=5)
        docs = [d for d in docs if (d.get("content") or "").strip()]
        if len(docs) < 3:
            print(f"[제외] 후보 문서 부족: {q[:40]}")
            continue
        prompt = build_rag_prompt(q, docs)
        docs_text = "\n---\n".join(
            f"{(d.get('metadata') or {}).get('title','')}: {(d.get('content') or '')[:400]}"
            for d in docs[:5])

        try:
            ans_oa, t_oa = generate(openai_llm, system_prompt, prompt)
            ans_ol, t_ol = generate(ollama_llm, system_prompt, prompt)
        except Exception as e:
            print(f"[제외] 생성 실패: {q[:40]} — {e}")
            continue

        flip = random.random() < 0.5  # 위치 편향 방지
        a, b = (ans_ol, ans_oa) if flip else (ans_oa, ans_ol)
        try:
            v = judge(q, docs_text, a, b)
        except Exception as e:
            print(f"[제외] 채점 실패: {q[:40]} — {e}")
            continue
        oa_key, ol_key = ("b", "a") if flip else ("a", "b")
        better = v.get("better", "tie")
        winner = ("openai" if better == oa_key else
                  "ollama" if better == ol_key else "tie")
        results.append({
            "q": q,
            "openai": {**v[oa_key], "latency_s": round(t_oa, 1)},
            "ollama": {**v[ol_key], "latency_s": round(t_ol, 1)},
            "winner": winner,
        })
        r = results[-1]
        print(f"■ {q[:44]}")
        print(f"  openai: {r['openai']['score']}점 hallu={r['openai']['hallucination']} {r['openai']['latency_s']}s | "
              f"ollama: {r['ollama']['score']}점 hallu={r['ollama']['hallucination']} {r['ollama']['latency_s']}s | 우세: {winner}")

    if not results:
        print("유효 결과 없음")
        return
    print("\n=== 요약 ===")
    for side in ["openai", "ollama"]:
        scores = [r[side]["score"] for r in results]
        hallu = sum(1 for r in results if r[side]["hallucination"])
        lat = [r[side]["latency_s"] for r in results]
        name = settings.OPENAI_CHAT_MODEL if side == "openai" else OLLAMA_CANDIDATE
        print(f"{name}: 평균 {sum(scores)/len(scores):.2f}점 | 할루 {hallu}/{len(results)}건 | "
              f"지연 avg {sum(lat)/len(lat):.1f}s / max {max(lat):.1f}s")
    wins = {w: sum(1 for r in results if r["winner"] == w) for w in ["openai", "ollama", "tie"]}
    print(f"우세: openai {wins['openai']} / ollama {wins['ollama']} / tie {wins['tie']}")


if __name__ == "__main__":
    main()
