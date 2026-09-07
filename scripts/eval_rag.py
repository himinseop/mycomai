"""RAG 평가 러너 CLI (#63 1단계).

golden.jsonl의 각 케이스에 대해 실제 rag_query를 호출하고 LLM-judge로 채점합니다.
핵심 로직(스코어링/집계/비교)은 순수 함수인 company_llm_rag.eval.runner에 있고,
이 스크립트는 rag_query와 judge LLM을 연결하는 얇은 CLI입니다.

사용법:
  # 전체 골든셋 평가
  python scripts/eval_rag.py

  # 일부만 (카테고리/개수/id 지정)
  python scripts/eval_rag.py --category manual --limit 10
  python scripts/eval_rag.py --ids man-coupon-001,abstain-003

  # 베이스라인과 비교
  python scripts/eval_rag.py --baseline tests/eval/results/20260901-000000.json

  # 검색은 다시 안 하고 judge만 재실행 (judge 프롬프트/모델 변경 후)
  python scripts/eval_rag.py --rejudge tests/eval/results/20260901-000000.json

Docker 실행 예 (CLAUDE.md 참고):
  docker run --rm -v "$PWD/src:/app" -v "$PWD/tests:/app/tests" -v "$PWD/scripts:/app/scripts" \\
    -v "$PWD/db:/app/db" -w /app -e PYTHONPATH=/app --env-file .env mycomai-rag \\
    python scripts/eval_rag.py
"""

import argparse
import json
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import company_llm_rag                                # noqa: E402
from company_llm_rag.config import settings          # noqa: E402
from company_llm_rag.eval import runner               # noqa: E402
from company_llm_rag.llm.openai_provider import OpenAIProvider  # noqa: E402

# 패키지 실제 위치 기준으로 찾음 — 로컬(src/company_llm_rag)과 컨테이너(/app/company_llm_rag,
# src/ 접두어 없이 마운트됨) 레이아웃이 달라 __file__ 기준 상대경로로는 못 찾는다.
_PROMPT_PATH = Path(company_llm_rag.__file__).resolve().parent / "prompts" / "eval_judge.txt"
_RESULTS_DIR = Path(__file__).resolve().parent.parent / "tests" / "eval" / "results"


def _load_judge_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _format_expected_facts(facts) -> str:
    if not facts:
        return "(없음)"
    return "\n".join(f"- {f}" for f in facts)


def make_judge_fn(model: str):
    """실제 OpenAI judge LLM을 호출하는 judge_fn(question, answer, context, expected_facts) -> str."""
    llm = OpenAIProvider(default_model=model, default_temperature=0.0)
    template = _load_judge_template()

    def judge_fn(question: str, answer: str, context: str, expected_facts) -> str:
        prompt = template.format(
            question=question,
            answer=answer,
            context=context or "(검색된 문서 없음)",
            expected_facts=_format_expected_facts(expected_facts),
        )
        return llm.chat([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=1500)

    return judge_fn


def run_case(case, judge_fn):
    """rag_query를 호출해 실제 답변을 얻고 채점합니다."""
    from company_llm_rag.rag_system import rag_query, _NO_ANSWER_PHRASE

    docs = []
    t0 = time.monotonic()
    answer, refs, timing = rag_query(case["question"], return_refs=True, _docs_out=docs)
    if timing.get("total_ms") is None:
        timing = dict(timing or {})
        timing["total_ms"] = int((time.monotonic() - t0) * 1000)

    return runner.score_case(
        case, answer, refs, docs, judge_fn,
        no_answer_phrase=_NO_ANSWER_PHRASE, timing=timing,
    )


def probe_chroma() -> bool:
    """ChromaDB 서버 접속 가능 여부만 확인(재시도 루프 없음)."""
    host = settings.CHROMA_SERVER_HOST
    port = settings.CHROMA_SERVER_PORT
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


def print_summary_table(results, summary):
    print("\n| id | category | faithfulness | correctness | abstain | source_hit |")
    print("|---|---|---|---|---|---|")
    for r in results:
        if r.get("query_error"):
            print(f"| {r['id']} | {r['category']} | (query_error) | - | - | - |")
            continue
        corr = f"{r['correctness']:.2f}" if r["correctness"] is not None else "N/A"
        hit = "-" if r["source_hit"] is None else ("O" if r["source_hit"] else "X")
        abstain = "O" if r["abstain_correct"] else "X"
        faith = f"{r['faithfulness']:.2f}" if r["faithfulness"] is not None else "N/A"
        print(f"| {r['id']} | {r['category']} | {faith} | {corr} | {abstain} | {hit} |")

    print("\n=== 집계 ===")
    print(f"케이스 수: {summary['n']}")
    print(f"평균 faithfulness: {summary['mean_faithfulness']:.3f}" if summary["mean_faithfulness"] is not None else "평균 faithfulness: N/A")
    if summary["mean_correctness"] is not None:
        print(f"평균 correctness: {summary['mean_correctness']:.3f}")
    else:
        print("평균 correctness: N/A (정답 사실 있는 케이스 없음)")
    print(f"abstain 정확도: {summary['abstain_accuracy']:.3f}" if summary["abstain_accuracy"] is not None else "abstain 정확도: N/A")
    if summary["source_hit_rate"] is not None:
        print(f"source_hit 비율: {summary['source_hit_rate']:.3f} (해당 케이스 {summary['source_hit_applicable_n']}건)")
    else:
        print("source_hit 비율: N/A")
    print(f"할루시네이션 claim 수: {summary['hallucinated_claims_total']} / 전체 claim {summary['claim_count_total']}")
    if summary["judge_parse_error_count"]:
        print(f"judge JSON 파싱 실패: {summary['judge_parse_error_count']}건")
    if summary.get("query_error_count"):
        print(f"검색/답변 실행 실패(query_error, 집계 제외): {summary['query_error_count']}건")
    if summary["mean_total_ms"] is not None:
        print(f"평균 총 소요시간: {summary['mean_total_ms']:.0f}ms")

    print("\n--- 카테고리별 ---")
    for cat, stats in summary["by_category"].items():
        f = f"{stats['mean_faithfulness']:.2f}" if stats["mean_faithfulness"] is not None else "N/A"
        c = f"{stats['mean_correctness']:.2f}" if stats["mean_correctness"] is not None else "N/A"
        a = f"{stats['abstain_accuracy']:.2f}" if stats["abstain_accuracy"] is not None else "N/A"
        sh = f"{stats['source_hit_rate']:.2f}" if stats["source_hit_rate"] is not None else "N/A"
        print(f"  {cat} (n={stats['n']}): faithfulness={f} correctness={c} abstain={a} source_hit={sh}")


def print_comparison(cmp):
    print("\n=== 베이스라인 비교 ===")
    print(f"회귀 케이스: {cmp['regression_count']}건")
    for c in cmp["regressions"]:
        print(f"  [회귀] {c['id']} ({c['category']}): {', '.join(c['reasons'])}")
    if cmp["new_case_ids"]:
        print(f"신규 케이스(베이스라인에 없음): {', '.join(cmp['new_case_ids'])}")
    if cmp["missing_case_ids"]:
        print(f"베이스라인에는 있으나 이번엔 빠짐: {', '.join(cmp['missing_case_ids'])}")
    query_error_ids = [c["id"] for c in cmp["per_case"] if c.get("status") == "query_error"]
    if query_error_ids:
        print(f"이번 실행에서 query_error(회귀 판정 제외): {', '.join(query_error_ids)}")


def main():
    parser = argparse.ArgumentParser(description="RAG 골든셋 평가 러너")
    parser.add_argument("--golden", default=str(Path(__file__).resolve().parent.parent / "tests" / "eval" / "golden.jsonl"))
    parser.add_argument("--ids", default="", help="콤마 구분 id 목록 — 지정 시 이 케이스만 실행")
    parser.add_argument("--category", default="", help="manual|hub|live|abstain — 지정 시 해당 카테고리만")
    parser.add_argument("--limit", type=int, default=0, help="0이면 무제한")
    parser.add_argument("--baseline", default="", help="비교할 이전 결과 JSON 경로")
    parser.add_argument("--out", default="", help="결과 저장 경로 (기본: tests/eval/results/{timestamp}.json)")
    parser.add_argument("--rejudge", default="", help="검색은 재실행하지 않고, 저장된 결과 파일을 judge만 다시 실행")
    parser.add_argument("--judge-model", default="", help="기본: settings.EVAL_JUDGE_MODEL")
    args = parser.parse_args()

    judge_model = args.judge_model or settings.EVAL_JUDGE_MODEL
    judge_fn = make_judge_fn(judge_model)

    golden = runner.load_golden(args.golden)
    by_id = {c["id"]: c for c in golden}

    if args.rejudge:
        with open(args.rejudge, "r", encoding="utf-8") as f:
            saved = json.load(f)
        saved_results = saved.get("results", saved) if isinstance(saved, dict) else saved
        results = []
        for sr in saved_results:
            case = by_id.get(sr["id"])
            if case is None:
                print(f"[스킵] golden.jsonl에서 사라진 id: {sr['id']}")
                continue
            results.append(runner.rejudge_case(sr, case, judge_fn))
    else:
        cases = golden
        if args.ids:
            wanted = set(args.ids.split(","))
            cases = [c for c in cases if c["id"] in wanted]
        if args.category:
            cases = [c for c in cases if c["category"] == args.category]
        if args.limit:
            cases = cases[: args.limit]

        if not cases:
            print("실행할 케이스가 없습니다 (필터 조건을 확인하세요).")
            return

        print(f"골든셋 {len(cases)}건 평가 시작 (judge model={judge_model})\n")
        results = []
        for i, case in enumerate(cases, start=1):
            print(f"[{i}/{len(cases)}] {case['id']}: {case['question'][:50]}")
            try:
                results.append(run_case(case, judge_fn))
            except Exception as e:
                print(f"  [오류] {e}")
                # rag_query 자체가 실패(예: OpenAI 429) — judge를 부르지도 못했으므로
                # judge_parse_error가 아니라 query_error로 분리 집계(집계에서 제외).
                results.append(runner.build_query_error_result(case, str(e)))

    summary = runner.summarize(results)
    print_summary_table(results, summary)

    comparison = None
    if args.baseline:
        with open(args.baseline, "r", encoding="utf-8") as f:
            base_saved = json.load(f)
        baseline_results = base_saved.get("results", base_saved) if isinstance(base_saved, dict) else base_saved
        comparison = runner.compare(results, baseline_results)
        print_comparison(comparison)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = Path(args.out) if args.out else _RESULTS_DIR / f"{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "golden_path": args.golden,
        "judge_model": judge_model,
        "summary": summary,
        "results": results,
    }
    if comparison is not None:
        payload["compared_baseline"] = args.baseline
        payload["comparison"] = comparison
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()
