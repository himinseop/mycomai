"""
거대 스프레드시트 raw-data 덤프의 오염 청크를 ChromaDB + FTS에서 제거합니다 (#55).

식별 기준: source=sharepoint, mime=스프레드시트(xls/xlsx), 문서당 청크 수 > THRESHOLD.
로우데이터/회원리스트/매장목록/취소이력 등 RAG 가치 없는 raw-data 덤프를 타겟합니다.
정상 문서(PDF/docx 가이드·규격서, _TC_ 테스트케이스)는 청크 수가 적어 영향 없음.

사용:
  PYTHONPATH=src python3 scripts/cleanup_spreadsheet_junk.py --dry-run
  PYTHONPATH=src python3 scripts/cleanup_spreadsheet_junk.py --threshold 1000
  (컨테이너) docker exec docker-web-1 python3 /app/.../cleanup_spreadsheet_junk.py --dry-run
"""
import argparse
import sqlite3
from collections import Counter

from company_llm_rag.config import settings
from company_llm_rag.database import db_manager


def _is_sheet(mime: str) -> bool:
    return ("spreadsheet" in mime) or ("ms-excel" in mime)


def _base_id(chunk_id: str) -> str:
    return chunk_id.rsplit("-chunk-", 1)[0]


def find_junk_docs(collection, threshold: int):
    """(doc_id, chunk_count, title) 리스트 반환."""
    all_ids = collection.get(include=[])["ids"]
    sp_ids = [i for i in all_ids if i.startswith("sharepoint-")]
    cnt = Counter(_base_id(i) for i in sp_ids)
    cand = [(d, n) for d, n in cnt.items() if n > threshold]
    # mime/title 조회
    sample = [d + "-chunk-0" for d, _ in cand]
    res = collection.get(ids=sample, include=["metadatas"])
    info = {
        mid.rsplit("-chunk-", 1)[0]: (m.get("mime_type", "") or "", m.get("title", "") or "")
        for mid, m in zip(res["ids"], res["metadatas"])
    }
    junk = [(d, n, info.get(d, ("", ""))[1]) for d, n in cand if _is_sheet(info.get(d, ("", ""))[0])]
    junk.sort(key=lambda x: -x[1])
    return junk, sp_ids, all_ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=1000, help="스프레드시트 청크 수 임계")
    ap.add_argument("--dry-run", action="store_true", help="삭제 없이 대상만 출력")
    ap.add_argument("--batch", type=int, default=5000)
    args = ap.parse_args()

    collection = db_manager.get_collection()
    junk, sp_ids, all_ids = find_junk_docs(collection, args.threshold)

    junk_set = {d for d, _, _ in junk}
    del_chunk_ids = [i for i in all_ids if _base_id(i) in junk_set]

    print(f"=== 삭제 대상 (스프레드시트 + 청크>{args.threshold}) ===")
    for d, n, title in junk:
        print(f"{n:7d}청크  {title[:50]}")
    print(f"\n문서 {len(junk)}개 / 청크 {len(del_chunk_ids):,}개")
    print(f"컬렉션 {collection.count():,} → {collection.count() - len(del_chunk_ids):,}")

    if args.dry_run:
        print("\n[dry-run] 삭제하지 않았습니다.")
        return

    # 1) ChromaDB 삭제 (배치)
    for i in range(0, len(del_chunk_ids), args.batch):
        collection.delete(ids=del_chunk_ids[i:i + args.batch])
        print(f"  ChromaDB 삭제 {min(i + args.batch, len(del_chunk_ids)):,}/{len(del_chunk_ids):,}")

    # 2) FTS 삭제 (chunk_id prefix)
    con = sqlite3.connect(settings.SEARCH_INDEX_DB_PATH)
    try:
        removed = 0
        for d in junk_set:
            cur = con.execute("DELETE FROM doc_fts WHERE chunk_id LIKE ?", (d + "-chunk-%",))
            removed += cur.rowcount
        con.commit()
        print(f"  FTS 삭제 {removed:,}행")
    finally:
        con.close()

    print(f"\n완료. 컬렉션 최종: {collection.count():,}")


if __name__ == "__main__":
    main()
