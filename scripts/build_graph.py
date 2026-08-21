"""
Jira 그래프 초기/수동 구축 (#59 Phase 1)

수집된 data/jira_data.jsonl로 그래프를 재구축합니다.
야간 수집에서는 data_loader가 적재 후 자동 호출하므로, 이 스크립트는
최초 구축이나 수동 재구축용입니다.

사용 (Docker):
  docker-compose -f docker/docker-compose.yml run --rm --no-deps data-loader \
      python3 scripts/build_graph.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from company_llm_rag.graph import graph_store, jira_graph  # noqa: E402


def main():
    jsonl = Path("data/jira_data.jsonl")
    if not jsonl.is_file():
        # Docker 내부 경로 (/app 기준) 폴백
        jsonl = Path("/app/data/jira_data.jsonl")
    if not jsonl.is_file():
        print(f"jira_data.jsonl을 찾을 수 없습니다: {jsonl}")
        sys.exit(1)

    stats = jira_graph.rebuild_from_jsonl(str(jsonl))
    print(f"재구축 완료: {stats}")
    print(f"그래프 현황: {graph_store.get_stats()}")


if __name__ == "__main__":
    main()
