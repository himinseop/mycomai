"""
Jira JSONL → 그래프 적재 (#59 Phase 1)

Jira는 LLM 추출 없이 이미 그래프입니다. 수집 JSONL의 메타데이터 필드만으로
이슈·담당자·프로젝트·라벨 노드와 관계 엣지를 만듭니다.

data_loader가 적재 후 호출하거나, scripts/build_graph.py로 단독 실행합니다.
"""

import json
import re
from typing import Dict, Iterable, List, Tuple

from company_llm_rag.graph import graph_store
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

NODE_TYPES = ["issue", "person", "project", "label"]

# "WMPO-40 (causes), ABC-1 (blocks)" 파싱
_LINK_RE = re.compile(r"([A-Z][A-Z0-9]+-\d+)(?:\s*\(([^)]*)\))?")


def _issue_rows(doc: Dict) -> Tuple[List[tuple], List[tuple]]:
    """수집 문서 1건 → (nodes, edges) 행 목록."""
    meta = doc.get("metadata", {}) or {}
    key = (meta.get("jira_issue_key") or "").upper()
    if not key:
        return [], []

    project = (meta.get("jira_project_key") or key.split("-")[0]).upper()
    title = doc.get("title", "") or key
    # 제목 앞 "[KEY] " 중복 제거
    if title.startswith(f"[{key}]"):
        title = title[len(key) + 2:].strip()

    issue_id = f"issue:{key}"
    nodes: List[tuple] = [(
        issue_id, "issue", title,
        json.dumps({
            "project": project,
            "status": meta.get("status", ""),
            "issue_type": meta.get("jira_issue_type", ""),
            "priority": meta.get("priority", ""),
            "assignee": meta.get("assignee", ""),
            "created_at": doc.get("created_at", ""),
            "updated_at": doc.get("updated_at", ""),
            "url": doc.get("url", ""),
            # 본문 앞부분 — 키워드 검색 보조 (전문은 ChromaDB에 있음)
            "summary_text": (doc.get("content") or "")[:500],
        }, ensure_ascii=False),
    )]
    edges: List[tuple] = []

    nodes.append((f"project:{project}", "project", project, "{}"))
    edges.append((f"project:{project}", issue_id, "HAS_ISSUE", "{}"))

    for field, rel in (("assignee", "ASSIGNED_TO"), ("reporter", "REPORTED_BY")):
        person = (meta.get(field) or "").strip()
        if person:
            nodes.append((f"person:{person}", "person", person, "{}"))
            edges.append((issue_id, f"person:{person}", rel, "{}"))

    for lb in (meta.get("labels") or "").split(","):
        lb = lb.strip()
        if lb:
            nodes.append((f"label:{lb}", "label", lb, "{}"))
            edges.append((issue_id, f"label:{lb}", "HAS_LABEL", "{}"))

    for m in _LINK_RE.finditer(meta.get("linked_issues") or ""):
        other, link_type = m.group(1).upper(), (m.group(2) or "").strip()
        edges.append((
            issue_id, f"issue:{other}", "LINKS_TO",
            json.dumps({"link_type": link_type}, ensure_ascii=False),
        ))

    return nodes, edges


def rebuild_from_docs(docs: Iterable[Dict]) -> Dict[str, int]:
    """
    Jira 수집 문서(iterable of dict)로 그래프를 전체 재구축합니다.

    링크 대상 이슈가 수집 범위 밖이면 노드 없는 엣지가 남을 수 있어,
    대상 노드가 없는 LINKS_TO 엣지는 placeholder 노드를 만들어 보존합니다.
    """
    node_map: Dict[str, tuple] = {}
    edge_map: Dict[tuple, tuple] = {}

    count = 0
    for doc in docs:
        if doc.get("source") != "jira" or doc.get("content_type") != "issue":
            continue
        nodes, edges = _issue_rows(doc)
        for n in nodes:
            # 이슈 노드(메타 있는 쪽)가 placeholder를 덮도록 나중 값 우선
            if n[0] not in node_map or n[1] == "issue":
                node_map[n[0]] = n
        for e in edges:
            edge_map[(e[0], e[1], e[2])] = e
        count += 1

    # LINKS_TO 대상 중 미수집 이슈 → placeholder 노드
    for (src, dst, rel), _e in list(edge_map.items()):
        if rel == "LINKS_TO" and dst not in node_map:
            key = dst.split(":", 1)[1]
            node_map[dst] = (dst, "issue", key, json.dumps(
                {"project": key.split("-")[0], "placeholder": True}))

    stats = graph_store.rebuild(node_map.values(), edge_map.values(), NODE_TYPES)
    stats["issues"] = count
    logger.info(f"[Graph] Jira 이슈 {count:,}건 반영")
    return stats


def rebuild_from_jsonl(path: str) -> Dict[str, int]:
    """jira_data.jsonl 파일에서 그래프를 재구축합니다."""
    def _iter():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    return rebuild_from_docs(_iter())
