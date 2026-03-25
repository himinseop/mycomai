#!/usr/bin/env python3
"""Local helper for inspecting query_history.db without docker exec."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _default_db_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / "db" / "query_history.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _load_record(con: sqlite3.Connection, record_id: int):
    row = con.execute(
        """
        SELECT id, session_id, created_at, question, answer,
               references_json, ref_count, ref_sources_json,
               response_time_ms, is_no_answer, feedback,
               no_answer_analysis, analysis_status, perf_json
          FROM query_history
         WHERE id = ?
        """,
        (record_id,),
    ).fetchone()
    return dict(row) if row else None


def _load_tail(con: sqlite3.Connection, limit: int):
    rows = con.execute(
        """
        SELECT id, created_at, session_id, question, is_no_answer, feedback
          FROM query_history
         ORDER BY id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _fmt_bytes(path: Path) -> str:
    if not path.exists():
        return "missing"
    size = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024
    return f"{size:.1f}GB"


def _print_record(record: dict, show_analysis: bool) -> None:
    references = json.loads(record["references_json"] or "[]")
    ref_sources = json.loads(record["ref_sources_json"] or "[]")
    perf = json.loads(record["perf_json"]) if record["perf_json"] else None

    print(f"id: {record['id']}")
    print(f"session_id: {record['session_id']}")
    print(f"created_at: {record['created_at']}")
    print(f"question: {record['question']}")
    print("answer:")
    print(record["answer"])
    print(f"ref_count: {record['ref_count']}")
    print(f"ref_sources: {json.dumps(ref_sources, ensure_ascii=False)}")
    print(f"is_no_answer: {bool(record['is_no_answer'])}")
    print(f"feedback: {record['feedback']}")
    print(f"analysis_status: {record['analysis_status']}")
    print(f"response_time_ms: {record['response_time_ms']}")
    if perf is not None:
        print(f"perf: {json.dumps(perf, ensure_ascii=False)}")
    if references:
        print("references:")
        print(json.dumps(references, ensure_ascii=False, indent=2))
    if show_analysis and record["no_answer_analysis"]:
        print("no_answer_analysis:")
        print(record["no_answer_analysis"])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect query_history.db from the host workspace."
    )
    parser.add_argument("record_id", nargs="?", type=int, help="query_history.id to load")
    parser.add_argument("--db-path", default=str(_default_db_path()), help="SQLite DB path")
    parser.add_argument("--tail", type=int, default=0, help="show latest N records")
    parser.add_argument("--json", action="store_true", help="print raw JSON for a record")
    parser.add_argument("--analysis", action="store_true", help="include no_answer_analysis")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    wal_path = db_path.with_name(db_path.name + "-wal")
    shm_path = db_path.with_name(db_path.name + "-shm")

    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    print(f"db: {db_path}")
    print(f"wal: {wal_path} ({_fmt_bytes(wal_path)})")
    print(f"shm: {shm_path} ({_fmt_bytes(shm_path)})")

    con = _connect(db_path)

    if args.tail:
        rows = _load_tail(con, args.tail)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if args.record_id is None:
        parser.error("record_id or --tail is required")

    record = _load_record(con, args.record_id)
    if not record:
        print("NOT_FOUND")
        return 2

    if args.json:
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0

    _print_record(record, show_analysis=args.analysis)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
