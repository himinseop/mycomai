"""
내용(content_hash)이 완전 동일한 중복 SharePoint 문서를 ChromaDB + FTS에서 제거합니다 (#55).

같은 제목이라도 내용이 다르면(예: 날짜별 데일리보고) 유지합니다. 각 제목 그룹에서
content_hash 집합이 동일한 문서는 청크가 가장 많은 1개만 남기고 나머지를 삭제합니다.

사용:
  docker exec knowledge-hub-web-1 python3 /path/dedup_identical_docs.py --dry-run
  docker exec knowledge-hub-web-1 python3 /path/dedup_identical_docs.py
"""
import argparse
import sqlite3
from collections import defaultdict

from company_llm_rag.config import settings
from company_llm_rag.database import db_manager


def find_identical_dups(collection):
    r = collection.get(where={"source": "sharepoint"}, include=["metadatas"], limit=1_000_000)
    docs = defaultdict(lambda: [None, set(), 0])  # doc_id -> [title, hashes, chunks]
    for m in r["metadatas"]:
        did = m.get("original_doc_id", "")
        if not did:
            continue
        docs[did][0] = m.get("title", "") or docs[did][0]
        h = m.get("content_hash")
        if h:
            docs[did][1].add(h)
        docs[did][2] += 1

    by_title = defaultdict(list)
    for did, (t, hs, n) in docs.items():
        by_title[(t or "").strip()].append((did, frozenset(hs), n))

    del_ids = []
    for _t, lst in by_title.items():
        if len(lst) < 2:
            continue
        seen = {}
        for did, hs, n in sorted(lst, key=lambda x: -x[2]):  # 청크 많은 것 유지
            if not hs:
                continue
            if hs in seen:
                del_ids.append(did)  # 내용 동일 → 삭제
            else:
                seen[hs] = did
    return set(del_ids)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    c = db_manager.get_collection()
    del_docids = find_identical_dups(c)
    r = c.get(where={"source": "sharepoint"}, include=["metadatas"], limit=1_000_000)
    del_ids = [cid for cid, m in zip(r["ids"], r["metadatas"])
               if m.get("original_doc_id", "") in del_docids]
    print(f"내용 동일 중복 문서 {len(del_docids)}개 / 청크 {len(del_ids):,}")
    print(f"컬렉션 {c.count():,} → {c.count() - len(del_ids):,}")
    if args.dry_run:
        print("[dry-run] 삭제하지 않았습니다.")
        return

    for i in range(0, len(del_ids), 5000):
        c.delete(ids=del_ids[i:i + 5000])
    con = sqlite3.connect(settings.SEARCH_INDEX_DB_PATH)
    removed = 0
    for did in del_docids:
        removed += con.execute("DELETE FROM doc_fts WHERE chunk_id LIKE ?", (did + "-chunk-%",)).rowcount
    con.commit()
    con.close()
    print(f"삭제 완료. 컬렉션 {c.count():,}, FTS {removed:,}행")


if __name__ == "__main__":
    main()
