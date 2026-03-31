import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger
from company_llm_rag.data_extraction.m365.auth import get_access_token, call_graph_api
from company_llm_rag.data_extraction.m365.file_parser import (
    extract_pdf_text,
    extract_pptx_text,
    extract_docx_text,
    extract_doc_text,
    extract_xlsx_text,
    extract_xls_text,
)

from company_llm_rag.data_extraction.common import fmt_elapsed as _fmt_elapsed

logger = get_logger(__name__)

_PROGRESS_EVERY = 10
_SKIP_FOLDER_NAMES = {"old", "version history", "archived"}

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

def get_site_id_for_teams_group(team_name: str, access_token: str) -> Optional[str]:
    """
    Teams 그룹명으로 연결된 SharePoint 사이트 ID를 반환합니다.

    Microsoft 365 그룹(Teams)은 자동으로 연결된 SharePoint 사이트를 갖습니다.
    Graph API: GET /groups/{group_id}/sites/root

    Args:
        team_name: Teams 그룹 displayName
        access_token: 액세스 토큰

    Returns:
        SharePoint 사이트 ID, 탐색 실패 시 None
    """
    filter_query = (
        f"displayName eq '{team_name}' "
        f"and resourceProvisioningOptions/any(x:x eq 'Team')"
    )
    try:
        data = call_graph_api(
            f"https://graph.microsoft.com/v1.0/groups"
            f"?$filter={requests.utils.quote(filter_query)}&$select=id,displayName",
            access_token,
        )
        groups = data.get("value", [])
        if not groups:
            logger.warning(f"Teams 그룹을 찾을 수 없습니다: '{team_name}'")
            return None
        group_id = groups[0]["id"]
        logger.info(f"Teams 그룹 ID 확인: {team_name} → {group_id}")
    except Exception as e:
        logger.warning(f"Teams 그룹 ID 조회 실패: {e}")
        return None

    try:
        site_data = call_graph_api(
            f"https://graph.microsoft.com/v1.0/groups/{group_id}/sites/root",
            access_token,
        )
        site_id = site_data.get("id")
        site_display = site_data.get("displayName") or site_data.get("name", "")
        logger.info(f"Teams 연결 SharePoint 사이트 발견: {site_display} ({site_id})")
        return site_id
    except Exception as e:
        logger.warning(f"Teams 그룹의 SharePoint 사이트 조회 실패: {e}")
        return None


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


def _should_skip_folder(folder_path: str, folder_name: str) -> bool:
    """수집 대상에서 제외할 폴더인지 판정합니다."""
    normalized_name = folder_name.strip().lower()
    if normalized_name in _SKIP_FOLDER_NAMES:
        return True

    normalized_path = f"{folder_path}/{folder_name}".lower().replace("\\", "/")
    return "/archived/" in normalized_path

def get_files_in_folder(drive_id: str, folder_path: str, access_token: str, _depth: int = 0) -> List[Dict]:
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
    _MAX_FOLDER_DEPTH = 15
    if _depth > _MAX_FOLDER_DEPTH:
        logger.warning(f"최대 폴더 깊이({_MAX_FOLDER_DEPTH}) 초과, 탐색 중단: {folder_path}")
        return []

    all_files_metadata = []

    # Base URL for children
    if folder_path == "" or folder_path == "/":
        base_endpoint = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/children"
    else:
        encoded_folder_path = requests.utils.quote(folder_path.lstrip('/'))
        base_endpoint = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{encoded_folder_path}:/children"

    endpoint = base_endpoint
    while endpoint:
        response_data = call_graph_api(endpoint, access_token)
        items = response_data.get('value', [])

        for item in items:
            if 'file' in item:
                all_files_metadata.append(item)
            elif 'folder' in item:
                # 폴더는 날짜 필터 없이 항상 재귀 탐색
                if _should_skip_folder(folder_path, item['name']):
                    logger.info(f"  - Skipping '{item['name']}' folder: {os.path.join(folder_path, item['name'])}")
                    continue
                new_folder_path = os.path.join(folder_path, item['name'])
                try:
                    all_files_metadata.extend(get_files_in_folder(drive_id, new_folder_path, access_token, _depth + 1))
                except Exception as e:
                    logger.warning(f"  - 폴더 탐색 실패, 건너뜁니다: {new_folder_path} | {e}")

        endpoint = response_data.get('@odata.nextLink')

    return all_files_metadata

def download_file_content(download_url: str, access_token: str) -> str:
    """
    주어진 다운로드 URL에서 텍스트 파일 콘텐츠를 다운로드합니다.

    Args:
        download_url: 파일 다운로드 URL
        access_token: 액세스 토큰

    Returns:
        파일 콘텐츠 (텍스트)
    """
    headers = {'Authorization': f'Bearer {access_token}'}
    response = requests.get(download_url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def download_file_bytes(download_url: str, access_token: str) -> bytes:
    """
    주어진 다운로드 URL에서 바이너리 파일을 다운로드합니다. (PDF, PPTX 등)

    Args:
        download_url: 파일 다운로드 URL
        access_token: 액세스 토큰

    Returns:
        파일 바이너리 데이터
    """
    headers = {'Authorization': f'Bearer {access_token}'}
    response = requests.get(download_url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.content


def _get_pptx_base_and_version(filename: str) -> Tuple[str, int]:
    """
    PPTX 파일명에서 기본명과 버전 번호를 추출합니다.

    Examples:
        '기획서_v3.pptx' → ('기획서', 3)
        'report_V2.pptx' → ('report', 2)
        '보고서.pptx'    → ('보고서', 0)
    """
    name = os.path.splitext(filename)[0]
    m = re.search(r'_v(\d+)$', name, re.IGNORECASE)
    if m:
        return name[:m.start()], int(m.group(1))
    return name, 0


def deduplicate_pptx_versions(files: List[Dict]) -> List[Dict]:
    """
    PPTX 파일 중 같은 폴더 + 같은 기본 파일명인 경우 최신 버전(_v숫자 가장 높은 것)만 유지합니다.
    버전 패턴이 없는 PPTX나 다른 형식 파일은 그대로 통과합니다.
    """
    pptx_mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    pptx_files = [f for f in files if f.get('file', {}).get('mimeType') == pptx_mime]
    other_files = [f for f in files if f.get('file', {}).get('mimeType') != pptx_mime]

    groups: Dict[Tuple[str, str], Tuple[Dict, int]] = {}
    for f in pptx_files:
        folder = f.get('parentReference', {}).get('path', '')
        base, version = _get_pptx_base_and_version(f['name'])
        key = (folder, base)
        if key not in groups or version > groups[key][1]:
            groups[key] = (f, version)

    latest_pptx = [item for item, _ in groups.values()]
    skipped = len(pptx_files) - len(latest_pptx)
    if skipped > 0:
        logger.info(f"PPTX 버전 중복 제거: {skipped}개 스킵, {len(latest_pptx)}개 유지")

    return other_files + latest_pptx

def main():
    try:
        access_token = get_access_token()
        logger.info("Successfully acquired access token.")

        # site_map: site_name → site_id (이미 ID를 알고 있는 경우 API 재조회 생략)
        site_map: Dict[str, str] = {}
        # seen_ids: 동일 사이트 중복 수집 방지
        seen_ids: set = set()
        target_sites: List[str] = []

        # 규칙 1: SHAREPOINT_SITE_NAME이 명시된 경우 해당 사이트 수집
        if settings.SHAREPOINT_SITE_NAME:
            target_sites.append(settings.SHAREPOINT_SITE_NAME)

        # 규칙 2: TEAMS_GROUP_NAMES + KNOWLEDGE_HUB_TEAM_NAME의 연결된 SharePoint 사이트도 수집
        all_team_names = list(settings.TEAMS_GROUP_NAMES)
        if settings.KNOWLEDGE_HUB_TEAM_NAME:
            hub = settings.KNOWLEDGE_HUB_TEAM_NAME
            if hub not in all_team_names:
                all_team_names.append(hub)
        for team_name in all_team_names:
            logger.info(f"Teams 그룹 '{team_name}'의 연결 SharePoint 사이트 탐색 중...")
            linked_site_id = get_site_id_for_teams_group(team_name, access_token)
            if linked_site_id:
                site_map[team_name] = linked_site_id
                target_sites.append(team_name)
            else:
                logger.warning(f"Teams 그룹 '{team_name}'의 SharePoint 사이트를 찾을 수 없어 건너뜁니다.")

        if not target_sites:
            logger.info("SHAREPOINT_SITE_NAME, TEAMS_GROUP_NAME, KNOWLEDGE_HUB_TEAM_NAME이 모두 설정되지 않아 SharePoint 수집을 건너뜁니다.")
            return

        for i, site_name in enumerate(target_sites):
            logger.info(f"[{i+1}/{len(target_sites)}] Processing SharePoint site: {site_name}...")
            try:
                site_id = site_map.get(site_name) or get_sharepoint_site_id(site_name, access_token)
                if site_id in seen_ids:
                    logger.info(f"  - 이미 수집된 사이트({site_id}), 건너뜁니다.")
                    continue
                seen_ids.add(site_id)
                logger.info(f"  - Site ID: {site_id}")

                drive_id = get_drive_id_for_site(site_id, access_token)
                logger.info(f"  - Drive ID: {drive_id}")

                files_metadata = get_files_in_folder(drive_id, "", access_token)
                if files_metadata:
                    files_metadata = deduplicate_pptx_versions(files_metadata)
                    total = len(files_metadata)
                    logger.info(f"[SharePoint][{site_name}] {total}개 파일 발견. 수집 시작...")
                    start_time = time.time()

                    TEXT_MIME_TYPES = {
                        "text/plain",
                        "text/markdown",
                        "application/json",
                        "application/xml",
                    }
                    BINARY_PARSERS = {
                        "application/pdf": extract_pdf_text,
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation": extract_pptx_text,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_docx_text,
                        "application/msword": extract_doc_text,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": extract_xlsx_text,
                        "application/vnd.ms-excel": extract_xls_text,
                    }

                    for k, file_meta in enumerate(files_metadata, 1):
                        file_name = file_meta.get('name')
                        file_id = file_meta.get('id')
                        file_web_url = file_meta.get('webUrl')
                        file_download_url = file_meta.get('@microsoft.graph.downloadUrl')
                        file_path = file_meta.get('parentReference', {}).get('path')

                        content_to_store = None
                        mime_type = file_meta.get('file', {}).get('mimeType')
                        file_size = file_meta.get('size')

                        if not file_download_url:
                            content_to_store = "[Content not available for download]"
                        elif mime_type in BINARY_PARSERS:
                            try:
                                file_bytes = download_file_bytes(file_download_url, access_token)
                                content_to_store = BINARY_PARSERS[mime_type](file_bytes)
                            except Exception as e:
                                content_to_store = f"[Error downloading or parsing content: {e}]"
                                logger.warning(f"Could not download/parse content for {file_name}: {e}")
                        elif mime_type in TEXT_MIME_TYPES:
                            try:
                                content_to_store = download_file_content(file_download_url, access_token)
                            except Exception as e:
                                content_to_store = f"[Error downloading content: {e}]"
                                logger.warning(f"Could not download content for {file_name}: {e}")
                        else:
                            content_to_store = f"[Content not extracted: Unsupported MIME type {mime_type}]"
                            logger.warning(
                                f"미지원 MIME 타입 — 파일: {file_name} | "
                                f"경로: {file_path} | MIME: {mime_type} | URL: {file_web_url}"
                            )


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
                        if k % _PROGRESS_EVERY == 0 or k == total:
                            pct = int(k / total * 100)
                            elapsed = _fmt_elapsed(time.time() - start_time)
                            logger.info(f"[SharePoint][{site_name}] {k}/{total} ({pct}%) | 현재: {file_name} | 경과: {elapsed}")
                    logger.info(f"[SharePoint][{site_name}] 완료: {total}개 | 소요: {_fmt_elapsed(time.time() - start_time)}")
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
