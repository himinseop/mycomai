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

import json  # noqa: E402

from company_llm_rag.graph import entity_link, graph_store, jira_graph  # noqa: E402


def _find(name: str) -> Path:
    for base in (Path("data"), Path("/app/data")):
        p = base / name
        if p.is_file():
            return p
    print(f"{name}을 찾을 수 없습니다")
    sys.exit(1)


def main():
    stats = jira_graph.rebuild_from_jsonl(str(_find("jira_data.jsonl")))
    print(f"Jira 재구축: {stats}")

    conf_docs = []
    with open(_find("confluence_data.jsonl"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            conf_docs.append({"id": d.get("id"), "title": d.get("title"),
                              "url": d.get("url"), "updated_at": d.get("updated_at"),
                              "content": (d.get("content") or "")[:300]})
    print(f"엔티티 링크: {entity_link.rebuild_entities(conf_docs)}")
    print(f"그래프 현황: {graph_store.get_stats()}")


if __name__ == "__main__":
    main()
