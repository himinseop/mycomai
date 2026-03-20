import requests
import json
import sys
import time
from datetime import timedelta

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger
from company_llm_rag.data_extraction.html_utils import parse_confluence_storage_format

logger = get_logger(__name__)

_PROGRESS_EVERY = 50

def _fmt_elapsed(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


def get_all_spaces():
    """사용자가 접근 가능한 모든 스페이스를 가져옵니다."""
    all_spaces = []
    start_at = 0
    limit = 50
    headers = settings.get_auth_header("confluence")

    while True:
        url = f"{settings.CONFLUENCE_BASE_URL}/rest/api/space"
        params = {"start": start_at, "limit": limit}
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        results = data.get('results', [])
        if not results:
            break
        all_spaces.extend(results)
        start_at += len(results)
        if len(results) < limit:
            break
    return all_spaces


def get_spaces_by_label(label: str) -> list:
    """
    특정 레이블이 붙은 스페이스 키 목록을 반환합니다.
    Confluence v2 API의 커서 기반 페이지네이션을 사용합니다.

    Args:
        label: 검색할 스페이스 레이블

    Returns:
        스페이스 키 리스트
    """
    space_keys = []
    headers = settings.get_auth_header("confluence")
    # v2 API base URL: CONFLUENCE_BASE_URL에서 /wiki 이후 경로 처리
    base = settings.CONFLUENCE_BASE_URL.rstrip("/")
    url = f"{base}/api/v2/spaces"
    params = {"label": label, "limit": 50}

    while url:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        for space in data.get("results", []):
            key = space.get("key")
            if key:
                space_keys.append(key)

        next_link = data.get("_links", {}).get("next")
        if next_link:
            # next_link는 /wiki/api/v2/spaces?... 형태의 절대 경로
            base_domain = base.split("/wiki")[0]
            url = base_domain + next_link
            params = {}  # cursor가 next_link에 포함되어 있음
        else:
            url = None

    return space_keys

def get_space_display_name(space_key: str) -> str:
    """스페이스 키로 표시 이름을 조회합니다."""
    try:
        url = f"{settings.CONFLUENCE_BASE_URL}/rest/api/space/{space_key}"
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json().get("name", space_key)
    except Exception:
        return space_key


def get_confluence_pages_in_space(space_key):
    """
    스페이스의 페이지를 가져옵니다.
    LOOKBACK_DAYS가 설정된 경우 증분 업데이트를 수행합니다.

    Args:
        space_key: Confluence 스페이스 키

    Returns:
        페이지 리스트
    """
    all_pages = []
    start_at = 0
    limit = settings.CONFLUENCE_PAGE_LIMIT
    headers = settings.get_auth_header("confluence")

    while True:
        if settings.LOOKBACK_DAYS:
            # Use search API with CQL for date filtering
            url = f"{settings.CONFLUENCE_BASE_URL}/rest/api/content/search"
            cql = f"space = \"{space_key}\" AND type = \"page\" AND lastModified >= \"-{settings.LOOKBACK_DAYS}d\""
            params = {
                "cql": cql,
                "expand": "body.storage,version,history,ancestors",
                "start": start_at,
                "limit": limit
            }
        else:
            # Standard content API
            url = f"{settings.CONFLUENCE_BASE_URL}/rest/api/content"
            params = {
                "spaceKey": space_key,
                "expand": "body.storage,version,history,ancestors",
                "start": start_at,
                "limit": limit,
                "type": "page"
            }

        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        pages = data.get('results', [])
        size = data.get('size', 0)

        if not pages:
            logger.debug(f"No more pages found. Fetched {len(all_pages)} total.")
            break

        all_pages.extend(pages)
        start_at += len(pages)

        logger.debug(f"Fetched {len(pages)} pages, total so far: {len(all_pages)}")

        # Check if this is the last page
        # When size < limit, it means we've reached the end
        if size < limit:
            logger.debug(f"Reached last page: size ({size}) < limit ({limit})")
            break

    return all_pages

def get_confluence_comments_for_page(page_id):
    """
    주어진 Confluence 페이지의 모든 댓글을 가져옵니다.

    Args:
        page_id: Confluence 페이지 ID

    Returns:
        댓글 리스트
    """
    all_comments = []
    start_at = 0
    limit = 100  # Confluence API default and maximum is 100 for comments
    headers = settings.get_auth_header("confluence")

    while True:
        url = f"{settings.CONFLUENCE_BASE_URL}/rest/api/content/{page_id}/child/comment"
        params = {
            "expand": "body.storage,author",
            "start": start_at,
            "limit": limit
        }
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        comments = data.get('results', [])

        if not comments:
            break

        all_comments.extend(comments)
        start_at += len(comments)

        # Simplified pagination logic
        if start_at >= data.get('total', 0):
            break

    return all_comments

def main():
    target_spaces = list(settings.CONFLUENCE_SPACE_KEYS)  # 명시적 스페이스 키

    # 레이블 기반 스페이스 추가
    if settings.CONFLUENCE_SPACE_LABELS:
        seen = set(target_spaces)
        for label in settings.CONFLUENCE_SPACE_LABELS:
            logger.info(f"레이블 '{label}'로 스페이스 조회 중...")
            try:
                label_spaces = get_spaces_by_label(label)
                added = [k for k in label_spaces if k not in seen]
                target_spaces.extend(added)
                seen.update(added)
                logger.info(f"레이블 '{label}': {len(label_spaces)}개 발견, {len(added)}개 신규 추가")
            except Exception as e:
                logger.error(f"레이블 '{label}' 스페이스 조회 실패: {e}", exc_info=True)

    if not target_spaces:
        logger.info("No CONFLUENCE_SPACE_KEY or CONFLUENCE_SPACE_LABELS specified. Discovering all accessible spaces...")
        try:
            spaces = get_all_spaces()
            target_spaces = [s['key'] for s in spaces]
            logger.info(f"Discovered {len(target_spaces)} spaces: {', '.join(target_spaces)}")
        except Exception as e:
            logger.error(f"Error discovering spaces: {e}", exc_info=True)
            return

    for i, space_key in enumerate(target_spaces):
        logger.info(f"[{i+1}/{len(target_spaces)}] Processing Confluence space: {space_key}...")
        try:
            space_display_name = get_space_display_name(space_key)
            pages_data = get_confluence_pages_in_space(space_key)
            if pages_data:
                total = len(pages_data)
                logger.info(f"[Confluence][{space_key}] {total}개 페이지 발견. 수집 시작...")
                start_time = time.time()
                for j, page in enumerate(pages_data, 1):
                    page_content = parse_confluence_storage_format(
                        page.get('body', {}).get('storage', {}).get('value', "")
                    )
                    if len(page_content.strip()) < 50:
                        logger.debug(f"[Confluence][{space_key}] 내용 부족 스킵: {page.get('title')}")
                        continue

                    extracted_data_schema = {
                        "id": f"confluence-{page.get('id')}",
                        "source": "confluence",
                        "source_id": page.get('id'),
                        "url": f"{settings.CONFLUENCE_BASE_URL}{page.get('_links', {}).get('webui')}",
                        "title": page.get('title'),
                        "content": page_content,
                        "content_type": "page",
                        "created_at": page.get('history', {}).get('createdDate'),
                        "updated_at": page.get('version', {}).get('when'),
                        "author": page.get('history', {}).get('createdBy', {}).get('displayName'),
                        "metadata": {
                            "confluence_space_key": space_key,
                            "confluence_space_name": space_display_name,
                            "confluence_ancestors": " / ".join(
                                a.get("title", "") for a in page.get("ancestors", []) if a.get("title")
                            ),
                            "last_updated_author": page.get('version', {}).get('by', {}).get('displayName'),
                            "comments": []
                        }
                    }

                    # Fetch comments for the page
                    comments_data = get_confluence_comments_for_page(page.get('id'))
                    for comment in comments_data:
                        extracted_data_schema["metadata"]["comments"].append({
                            "id": comment.get('id'),
                            "author": comment.get('author', {}).get('displayName'),
                            "created_at": comment.get('history', {}).get('createdDate'),
                            "content": parse_confluence_storage_format(
                                comment.get('body', {}).get('storage', {}).get('value', "")
                            )
                        })
                    
                    print(json.dumps(extracted_data_schema, ensure_ascii=False))
                    if j % _PROGRESS_EVERY == 0 or j == total:
                        pct = int(j / total * 100)
                        elapsed = _fmt_elapsed(time.time() - start_time)
                        logger.info(f"[Confluence][{space_key}] {j}/{total} ({pct}%) | 경과: {elapsed}")
                logger.info(f"[Confluence][{space_key}] 완료: {total}개 | 소요: {_fmt_elapsed(time.time() - start_time)}")
            else:
                logger.warning(f"No pages found for Confluence space {space_key}.")
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching Confluence data for space {space_key}: {e}")
            if e.response:
                logger.error(f"Response status: {e.response.status_code}")
                logger.debug(f"Response body: {e.response.text}")
        except Exception as e:
            logger.error(f"An unexpected error occurred for space {space_key}: {e}", exc_info=True)

if __name__ == "__main__":
    main()
