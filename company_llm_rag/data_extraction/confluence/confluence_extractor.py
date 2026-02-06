import requests
import os
import json
import base64 # Added for base64 encoding
from dotenv import load_dotenv # Added for loading .env file

load_dotenv() # Load environment variables from .env file

# Confluence Cloud URL (e.g., 'https://your-company.atlassian.net/wiki')
CONFLUENCE_BASE_URL = os.getenv('CONFLUENCE_BASE_URL')
# API Token generated from your Atlassian account (https://id.atlassian.com/manage-api/create-api-token)
CONFLUENCE_API_TOKEN = os.getenv('CONFLUENCE_API_TOKEN')
# Your Atlassian email
CONFLUENCE_EMAIL = os.getenv('CONFLUENCE_EMAIL')
# Confluence Space Key (e.g., 'O2OLAB')
CONFLUENCE_SPACE_KEY = os.getenv('CONFLUENCE_SPACE_KEY')

if not all([CONFLUENCE_BASE_URL, CONFLUENCE_API_TOKEN, CONFLUENCE_EMAIL, CONFLUENCE_SPACE_KEY]):
    print("Please set CONFLUENCE_BASE_URL, CONFLUENCE_API_TOKEN, CONFLUENCE_EMAIL, and CONFLUENCE_SPACE_KEY environment variables.")
    exit(1)

HEADERS = {
    "Accept": "application/json",
    "Authorization": f"Basic {base64.b64encode(f'{CONFLUENCE_EMAIL}:{CONFLUENCE_API_TOKEN}'.encode()).decode()}"
}

def get_confluence_pages_in_space(space_key):
    """
    Fetches all pages from a given Confluence space.
    """
    all_pages = []
    start_at = 0
    limit = 25 # Confluence API default and maximum is 25 for content search

    while True:
        url = f"{CONFLUENCE_BASE_URL}/rest/api/content"
        params = {
            "spaceKey": space_key,
            "expand": "body.storage,version,history,ancestors", # Include page content, version, history
            "start": start_at,
            "limit": limit,
            "type": "page" # Only fetch pages, not blog posts or other content types
        }
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        data = response.json()
        pages = data.get('results', [])
        
        if not pages:
            break
        
        all_pages.extend(pages)
        start_at += len(pages)

        if start_at >= data.get('size', 0) and data.get('size',0) < data.get('limit',0): # No more pages if current size is less than limit, assuming it's the last page
            break
        elif start_at >= data.get('size', 0) and data.get('size',0) == data.get('limit',0) and data.get('total',0) == start_at:
             break # No more pages if start_at equals total results
        elif start_at >= data.get('total', 0):
            break
            
    return all_pages

def get_confluence_comments_for_page(page_id):
    """
    Fetches all comments for a given Confluence page.
    """
    all_comments = []
    start_at = 0
    limit = 100 # Confluence API default and maximum is 100 for comments

    while True:
        url = f"{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}/child/comment"
        params = {
            "expand": "body.storage,author",
            "start": start_at,
            "limit": limit
        }
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        data = response.json()
        comments = data.get('results', [])

        if not comments:
            break

        all_comments.extend(comments)
        start_at += len(comments)

        if start_at >= data.get('size', 0) and data.get('size',0) < data.get('limit',0): # No more comments if current size is less than limit, assuming it's the last page
            break
        elif start_at >= data.get('size', 0) and data.get('size',0) == data.get('limit',0) and data.get('total',0) == start_at:
             break # No more comments if start_at equals total results
        elif start_at >= data.get('total', 0):
            break

    return all_comments

def main():
    print(f"Fetching Confluence pages from space: {CONFLUENCE_SPACE_KEY}...")
    try:
        pages_data = get_confluence_pages_in_space(CONFLUENCE_SPACE_KEY)
        if pages_data:
            for page in pages_data:
                extracted_data_schema = {
                    "id": f"confluence-{page.get('id')}",
                    "source": "confluence",
                    "source_id": page.get('id'),
                    "url": f"{CONFLUENCE_BASE_URL}{page.get('_links', {}).get('webui')}",
                    "title": page.get('title'),
                    "content": page.get('body', {}).get('storage', {}).get('value'),
                    "content_type": "page",
                    "created_at": page.get('history', {}).get('createdDate'),
                    "updated_at": page.get('version', {}).get('when'),
                    "author": page.get('history', {}).get('createdBy', {}).get('displayName'),
                    "metadata": {
                        "confluence_space_key": CONFLUENCE_SPACE_KEY,
                        "confluence_space_name": CONFLUENCE_SPACE_KEY, # Assuming space key is the name
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
            print(f"No pages found for Confluence space {CONFLUENCE_SPACE_KEY}.")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Confluence data: {e}")
        if e.response:
            print(f"Response status: {e.response.status_code}")
            print(f"Response body: {e.response.text}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    main()
