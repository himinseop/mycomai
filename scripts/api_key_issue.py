"""
인사이트 API 키 발급/폐기/목록 CLI (#56)

사용 (컨테이너):
  docker exec knowledge-hub-web-1 python3 /app/scripts/api_key_issue.py issue --name "매출대시보드" --scopes sales
  docker exec knowledge-hub-web-1 python3 /app/scripts/api_key_issue.py list
  docker exec knowledge-hub-web-1 python3 /app/scripts/api_key_issue.py revoke --id 1
  docker exec knowledge-hub-web-1 python3 /app/scripts/api_key_issue.py activate --id 1

사용 (로컬):
  PYTHONPATH=src python3 scripts/api_key_issue.py issue --name "테스트" --scopes sales,voc

주의: 원문 키는 발급 시 1회만 표시됩니다 (DB에는 SHA-256 해시만 저장).
"""
import argparse

from company_llm_rag.insight_api.store import (
    create_client, init_insight_db, list_clients, set_client_active,
)


def main():
    ap = argparse.ArgumentParser(description="인사이트 API 키 관리")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_issue = sub.add_parser("issue", help="새 클라이언트 키 발급")
    p_issue.add_argument("--name", required=True, help="클라이언트명")
    p_issue.add_argument("--scopes", required=True, help="허용 도메인 CSV (예: sales,voc)")
    p_issue.add_argument("--rate-limit", type=int, default=None, help="분당 호출 한도(기본값 사용 시 생략)")

    p_revoke = sub.add_parser("revoke", help="클라이언트 비활성화(즉시 차단)")
    p_revoke.add_argument("--id", type=int, required=True)

    p_activate = sub.add_parser("activate", help="클라이언트 재활성화")
    p_activate.add_argument("--id", type=int, required=True)

    sub.add_parser("list", help="클라이언트 목록")

    args = ap.parse_args()
    init_insight_db()

    if args.cmd == "issue":
        scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
        result = create_client(args.name, scopes, args.rate_limit)
        print(f"클라이언트 발급 완료 (id={result['id']}, scopes={','.join(scopes)})")
        print(f"\n  API Key: {result['api_key']}\n")
        print("⚠️  이 키는 지금 1회만 표시됩니다. 안전한 곳에 보관하세요.")
    elif args.cmd == "revoke":
        ok = set_client_active(args.id, False)
        print("비활성화 완료" if ok else f"id={args.id} 클라이언트 없음")
    elif args.cmd == "activate":
        ok = set_client_active(args.id, True)
        print("활성화 완료" if ok else f"id={args.id} 클라이언트 없음")
    elif args.cmd == "list":
        rows = list_clients()
        if not rows:
            print("등록된 클라이언트가 없습니다.")
            return
        print(f"{'id':>4}  {'active':6}  {'name':20}  {'scopes':20}  {'rate/min':8}  created_at")
        for r in rows:
            print(f"{r['id']:>4}  {('O' if r['is_active'] else 'X'):6}  {r['name']:20}  "
                  f"{r['scopes']:20}  {str(r['rate_limit_per_min'] or '-'):8}  {r['created_at'][:19]}")


if __name__ == "__main__":
    main()
