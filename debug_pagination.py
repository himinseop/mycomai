"""
페이지네이션 디버깅 스크립트
"""
import sys
import os
sys.path.insert(0, 'src')

from company_llm_rag.config import settings

# Jira 설정 확인
print("=== Jira Settings ===")
print(f"JIRA_MAX_RESULTS: {settings.JIRA_MAX_RESULTS}")
print(f"JIRA_BASE_URL: {settings.JIRA_BASE_URL}")
print(f"JIRA_PROJECT_KEYS: {settings.JIRA_PROJECT_KEYS}")

print("\n=== Confluence Settings ===")
print(f"CONFLUENCE_PAGE_LIMIT: {settings.CONFLUENCE_PAGE_LIMIT}")
print(f"CONFLUENCE_BASE_URL: {settings.CONFLUENCE_BASE_URL}")
print(f"CONFLUENCE_SPACE_KEYS: {settings.CONFLUENCE_SPACE_KEYS}")

# 실제 API 호출 시뮬레이션
print("\n=== Simulating Pagination Logic ===")

# Jira 시뮬레이션
print("\nJira pagination:")
start_at = 0
max_results = settings.JIRA_MAX_RESULTS
total = 100  # 가정

iteration = 0
while True:
    iteration += 1
    print(f"  Iteration {iteration}: start_at={start_at}, fetching {max_results} items")

    # 실제로는 API 호출
    fetched = min(max_results, total - start_at)
    print(f"    - Fetched {fetched} items")

    if fetched == 0:
        print("    - No more items, breaking")
        break

    start_at += fetched
    print(f"    - New start_at: {start_at}, total: {total}")

    if start_at >= total:
        print("    - Reached total, breaking")
        break

    if iteration > 10:
        print("    - Safety limit reached")
        break

# Confluence 시뮬레이션
print("\nConfluence pagination:")
start_at = 0
limit = settings.CONFLUENCE_PAGE_LIMIT
total = 100  # 가정

iteration = 0
while True:
    iteration += 1
    print(f"  Iteration {iteration}: start={start_at}, limit={limit}")

    # 실제로는 API 호출
    fetched = min(limit, total - start_at)
    print(f"    - Fetched {fetched} items")

    if fetched == 0:
        print("    - No more items, breaking")
        break

    start_at += fetched
    print(f"    - New start: {start_at}, total: {total}")

    if start_at >= total:
        print("    - Reached total, breaking")
        break

    if iteration > 10:
        print("    - Safety limit reached")
        break
