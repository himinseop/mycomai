import requests
import json
import sys

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

def get_all_spaces():
    """사용자가 접근 가능한 모든 스페이스를 가져옵니다."""
    all_spaces = []
    start_at = 0
    limit = 50
    headers = settings.get_auth_header("confluence")

    while True:
        url = f"{settings.CONFLUENCE_BASE_URL}/rest/api/space"
        params = {"start": start_at, "limit": limit}
        response = requests.get(url, headers=headers, params=params)
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

        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()

        pages = data.get('results', [])

        if not pages:
            break

        all_pages.extend(pages)
        start_at += len(pages)

        # Simplified pagination logic
        if start_at >= data.get('total', 0):
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
        response = requests.get(url, headers=headers, params=params)
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
    target_spaces = settings.CONFLUENCE_SPACE_KEYS

    if not target_spaces:
        logger.info("No CONFLUENCE_SPACE_KEY specified. Discovering all accessible spaces...")
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
            pages_data = get_confluence_pages_in_space(space_key)
            if pages_data:
                logger.info(f"  - Found {len(pages_data)} pages.")
                for page in pages_data:
                    extracted_data_schema = {
                        "id": f"confluence-{page.get('id')}",
                        "source": "confluence",
                        "source_id": page.get('id'),
                        "url": f"{settings.CONFLUENCE_BASE_URL}{page.get('_links', {}).get('webui')}",
                        "title": page.get('title'),
                        "content": page.get('body', {}).get('storage', {}).get('value'),
                        "content_type": "page",
                        "created_at": page.get('history', {}).get('createdDate'),
                        "updated_at": page.get('version', {}).get('when'),
                        "author": page.get('history', {}).get('createdBy', {}).get('displayName'),
                        "metadata": {
                            "confluence_space_key": space_key,
                            "confluence_space_name": space_key, # Assuming space key is the name
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
                            "content": comment.get('body', {}).get('storage', {}).get('value')
                        })
                    
                    print(json.dumps(extracted_data_schema, ensure_ascii=False))
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
