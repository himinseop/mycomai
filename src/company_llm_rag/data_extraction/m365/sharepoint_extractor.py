import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
from urllib.parse import urlparse

import msal
import requests

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

def get_msal_app() -> msal.ConfidentialClientApplication:
    """MSAL 애플리케이션 인스턴스를 생성합니다."""
    authority = f"https://login.microsoftonline.com/{settings.TENANT_ID}"
    return msal.ConfidentialClientApplication(
        settings.CLIENT_ID,
        authority=authority,
        client_credential=settings.CLIENT_SECRET
    )


def get_access_token() -> str:
    """
    Microsoft Graph API용 액세스 토큰을 획득합니다.

    Returns:
        액세스 토큰

    Raises:
        Exception: 토큰 획득 실패 시
    """
    app = get_msal_app()
    scope = ["https://graph.microsoft.com/.default"]
    result = app.acquire_token_for_client(scopes=scope)

    if "access_token" in result and result["access_token"]:
        return result["access_token"]
    else:
        logger.error(f"MSAL acquire_token_for_client result: {result}")
        error_msg = result.get('error_description') or result.get('error') or "Access token is empty or could not be acquired."
        raise Exception(f"Could not acquire access token: {error_msg}")

def call_graph_api(endpoint: str, access_token: str) -> Dict:
    """
    Microsoft Graph API에 GET 요청을 보냅니다.

    Args:
        endpoint: API 엔드포인트 URL
        access_token: 액세스 토큰

    Returns:
        API 응답 (JSON)
    """
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    response = requests.get(endpoint, headers=headers)
    response.raise_for_status()
    return response.json()

def get_all_sites(access_token: str) -> List[Dict]:
    """
    사용자/애플리케이션이 접근 가능한 모든 SharePoint 사이트를 가져옵니다.

    Args:
        access_token: 액세스 토큰

    Returns:
        사이트 리스트
    """
    endpoint = "https://graph.microsoft.com/v1.0/sites?search=*"
    results = call_graph_api(endpoint, access_token)
    return results.get('value', [])

def get_sharepoint_site_id(site_name: str, access_token: str) -> str:
    """
    이름으로 SharePoint 사이트 ID를 가져옵니다.

    Args:
        site_name: 사이트 이름
        access_token: 액세스 토큰

    Returns:
        사이트 ID
    """
    # 검색 엔드포인트를 사용하여 사이트 찾기
    try:
        search_endpoint = f"https://graph.microsoft.com/v1.0/sites?search='{site_name}'"
        search_results = call_graph_api(search_endpoint, access_token)
        sites = search_results.get('value', [])
        if sites:
            # 정확히 일치하는 이름 찾기
            for site in sites:
                if site.get('displayName').lower() == site_name.lower() or site.get('name').lower() == site_name.lower():
                    return site['id']
            # 정확히 일치하지 않으면 첫 번째 결과 반환
            return sites[0]['id']
    except requests.exceptions.HTTPError as e:
        logger.warning(f"SharePoint site search failed for '{site_name}': {e}. Falling back to hostname-based lookup.")

    # Fallback to original hostname-based lookup
    root_site_info = call_graph_api("https://graph.microsoft.com/v1.0/sites/root", access_token)
    hostname = urlparse(root_site_info['webUrl']).hostname

    endpoint = f"https://graph.microsoft.com/v1.0/sites/{hostname}:/sites/{site_name}"
    try:
        site_info = call_graph_api(endpoint, access_token)
        return site_info['id']
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            endpoint = f"https://graph.microsoft.com/v1.0/sites/{hostname}:/{site_name}"
            site_info = call_graph_api(endpoint, access_token)
            return site_info['id']
        else:
            raise

def get_drive_id_for_site(site_id: str, access_token: str) -> str:
    """
    SharePoint 사이트의 기본 문서 드라이브 ID를 가져옵니다.

    Args:
        site_id: 사이트 ID
        access_token: 액세스 토큰

    Returns:
        드라이브 ID
    """
    endpoint = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive"
    drive_info = call_graph_api(endpoint, access_token)
    return drive_info['id']

def get_files_in_folder(drive_id: str, folder_path: str, access_token: str) -> List[Dict]:
    """
    폴더 내의 파일을 재귀적으로 가져옵니다.
    LOOKBACK_DAYS가 설정된 경우 날짜로 필터링합니다.

    Args:
        drive_id: 드라이브 ID
        folder_path: 폴더 경로
        access_token: 액세스 토큰

    Returns:
        파일 메타데이터 리스트
    """
    all_files_metadata = []

    # Base URL for children
    if folder_path == "" or folder_path == "/":
        base_endpoint = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/children"
    else:
        encoded_folder_path = requests.utils.quote(folder_path.lstrip('/'))
        base_endpoint = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{encoded_folder_path}:/children"

    endpoint = base_endpoint
    if settings.LOOKBACK_DAYS:
        lookback_date = (datetime.now(timezone.utc) - timedelta(days=settings.LOOKBACK_DAYS)).isoformat()
        endpoint += f"?$filter=lastModifiedDateTime ge {lookback_date}"

    while endpoint:
        response_data = call_graph_api(endpoint, access_token)
        items = response_data.get('value', [])
        
        for item in items:
            if 'file' in item: 
                all_files_metadata.append(item)
            elif 'folder' in item: 
                new_folder_path = os.path.join(folder_path, item['name'])
                all_files_metadata.extend(get_files_in_folder(drive_id, new_folder_path, access_token))
        
        endpoint = response_data.get('@odata.nextLink') 

    return all_files_metadata

def download_file_content(download_url: str, access_token: str) -> str:
    """
    주어진 다운로드 URL에서 파일 콘텐츠를 다운로드합니다.

    Args:
        download_url: 파일 다운로드 URL
        access_token: 액세스 토큰

    Returns:
        파일 콘텐츠 (텍스트)
    """
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    response = requests.get(download_url, headers=headers)
    response.raise_for_status()
    return response.text

def main():
    try:
        access_token = get_access_token()
        logger.info("Successfully acquired access token.")

        target_sites = [settings.SHAREPOINT_SITE_NAME] if settings.SHAREPOINT_SITE_NAME else []

        if not target_sites:
            logger.info("No SHAREPOINT_SITE_NAME specified. Discovering all accessible sites...")
            try:
                sites = get_all_sites(access_token)
                target_sites = [s.get('displayName') or s.get('name') for s in sites]
                site_map = {(s.get('displayName') or s.get('name')): s['id'] for s in sites}
                logger.info(f"Discovered {len(target_sites)} sites: {', '.join(target_sites)}")
            except Exception as e:
                logger.error(f"Error discovering sites: {e}", exc_info=True)
                return
        else:
            site_map = {}

        for i, site_name in enumerate(target_sites):
            logger.info(f"[{i+1}/{len(target_sites)}] Processing SharePoint site: {site_name}...")
            try:
                site_id = site_map.get(site_name) or get_sharepoint_site_id(site_name, access_token)
                logger.info(f"  - Site ID: {site_id}")

                drive_id = get_drive_id_for_site(site_id, access_token)
                logger.info(f"  - Drive ID: {drive_id}")

                files_metadata = get_files_in_folder(drive_id, "", access_token)
                if files_metadata:
                    logger.info(f"  - Found {len(files_metadata)} files. Downloading content...")
                    for file_meta in files_metadata:
                        file_name = file_meta.get('name')
                        file_id = file_meta.get('id')
                        file_web_url = file_meta.get('webUrl')
                        file_download_url = file_meta.get('@microsoft.graph.downloadUrl')
                        file_path = file_meta.get('parentReference', {}).get('path')
                        
                        content_to_store = None
                        mime_type = file_meta.get('file', {}).get('mimeType')
                        file_size = file_meta.get('size')

                        # Attempt to download content only for supported text-based files
                        if file_download_url and mime_type in [
                            "text/plain", "text/markdown", "application/json", "application/xml",
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # .docx
                            "application/pdf" # For PDF
                        ]:
                            try:
                                file_content = download_file_content(file_download_url, access_token)
                                content_to_store = file_content
                            except Exception as e:
                                content_to_store = f"[Error downloading or parsing content: {e}]"
                                logger.warning(f"Could not download/parse content for {file_name}: {e}")
                        elif file_download_url:
                            content_to_store = f"[Content not extracted: Unsupported MIME type {mime_type}]"
                        else:
                            content_to_store = "[Content not available for download]"


                        extracted_data_schema = {
                            "id": f"sharepoint-{file_id}",
                            "source": "sharepoint",
                            "source_id": file_id,
                            "url": file_web_url,
                            "title": file_name,
                            "content": content_to_store,
                            "content_type": "file",
                            "created_at": file_meta.get('createdDateTime'),
                            "updated_at": file_meta.get('lastModifiedDateTime'),
                            "author": file_meta.get('lastModifiedBy', {}).get('user', {}).get('displayName'), # Using lastModifiedBy for author
                            "metadata": {
                                "sharepoint_site_name": site_name,
                                "sharepoint_file_path": file_path,
                                "mime_type": mime_type,
                                "size": file_size
                            }
                        }
                        
                        print(json.dumps(extracted_data_schema, ensure_ascii=False))
                else:
                    logger.warning(f"No files found in SharePoint site '{site_name}'.")
            except Exception as e:
                logger.error(f"Error processing SharePoint site '{site_name}': {e}", exc_info=True)

    except requests.exceptions.RequestException as e:
        logger.error(f"Error calling Microsoft Graph API: {e}")
        if e.response:
            logger.error(f"Response status: {e.response.status_code}")
            logger.debug(f"Response body: {e.response.text}")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)

if __name__ == "__main__":
    main()
