import json
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

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

def call_graph_api(endpoint: str, access_token: str, max_retries: int = 3) -> Dict:
    """
    Microsoft Graph API에 GET 요청을 보냅니다.
    429 Rate Limit 발생 시 Retry-After 헤더에 따라 재시도합니다.

    Args:
        endpoint: API 엔드포인트 URL
        access_token: 액세스 토큰
        max_retries: 최대 재시도 횟수

    Returns:
        API 응답 (JSON)
    """
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    for attempt in range(max_retries):
        response = requests.get(endpoint, headers=headers)

        if response.status_code == 429:
            retry_after = max(int(response.headers.get('Retry-After', 10)), 10)
            logger.warning(f"Rate limited. Retrying after {retry_after}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(retry_after)
            continue

        response.raise_for_status()
        return response.json()

    raise Exception(f"Max retries exceeded for endpoint: {endpoint}")

def get_all_teams(access_token: str) -> List[Dict]:
    """
    사용자/애플리케이션이 접근 가능한 모든 Teams를 가져옵니다.

    Args:
        access_token: 액세스 토큰

    Returns:
        Teams 리스트
    """
    endpoint = "https://graph.microsoft.com/v1.0/groups?$filter=resourceProvisioningOptions/any(x:x eq 'Team')&$select=id,displayName"
    results = call_graph_api(endpoint, access_token)
    return results.get('value', [])

def get_team_id_by_display_name(team_display_name: str, access_token: str) -> str:
    """
    표시 이름으로 Team ID를 가져옵니다.

    Args:
        team_display_name: 팀 표시 이름
        access_token: 액세스 토큰

    Returns:
        팀 ID

    Raises:
        Exception: 팀을 찾을 수 없는 경우
    """
    endpoint = f"https://graph.microsoft.com/v1.0/groups?$filter=displayName eq '{team_display_name}' and resourceProvisioningOptions/any(x:x eq 'Team')&$select=id,displayName"
    response_data = call_graph_api(endpoint, access_token)
    groups = response_data.get('value', [])
    if groups:
        return groups[0]['id']
    raise Exception(f"Team with display name '{team_display_name}' not found.")

def get_channels_for_team(team_id: str, access_token: str) -> List[Dict]:
    """
    주어진 팀의 모든 채널을 가져옵니다.

    Args:
        team_id: 팀 ID
        access_token: 액세스 토큰

    Returns:
        채널 리스트
    """
    all_channels = []
    endpoint = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels"

    while endpoint:
        response_data = call_graph_api(endpoint, access_token)
        channels = response_data.get('value', [])
        all_channels.extend(channels)
        endpoint = response_data.get('@odata.nextLink')
    return all_channels

def get_chat_info(chat_id: str, access_token: str) -> Dict:
    """
    채팅방 기본 정보(제목, 타입 등)를 가져옵니다.

    Args:
        chat_id: 채팅방 ID
        access_token: 액세스 토큰

    Returns:
        채팅방 정보
    """
    endpoint = f"https://graph.microsoft.com/v1.0/chats/{chat_id}?$select=id,chatType,topic,lastUpdatedDateTime"
    return call_graph_api(endpoint, access_token)


def get_direct_chat_messages(chat_id: str, access_token: str) -> List[Dict]:
    """
    채팅방 메시지를 직접 가져옵니다. (Chat.Read.All Application 권한 사용)
    LOOKBACK_DAYS가 설정된 경우 날짜로 필터링합니다.

    Args:
        chat_id: 채팅방 ID
        access_token: 액세스 토큰

    Returns:
        메시지 리스트
    """
    all_messages = []
    endpoint = f"https://graph.microsoft.com/v1.0/chats/{chat_id}/messages"

    if settings.LOOKBACK_DAYS:
        lookback_date = (datetime.now(timezone.utc) - timedelta(days=settings.LOOKBACK_DAYS)).isoformat()
        endpoint += f"?$filter=lastModifiedDateTime ge {lookback_date}"

    while endpoint:
        response_data = call_graph_api(endpoint, access_token)
        all_messages.extend(response_data.get('value', []))
        endpoint = response_data.get('@odata.nextLink')
        if endpoint:
            time.sleep(0.5)  # 페이지 간 딜레이로 Rate Limit 방지

    return all_messages


def get_channel_messages(team_id: str, channel_id: str, access_token: str) -> List[Dict]:
    """
    채널의 메시지를 가져옵니다.
    LOOKBACK_DAYS가 설정된 경우 날짜로 필터링합니다.

    Args:
        team_id: 팀 ID
        channel_id: 채널 ID
        access_token: 액세스 토큰

    Returns:
        메시지 리스트
    """
    all_messages = []
    endpoint = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}/messages?$expand=replies"

    if settings.LOOKBACK_DAYS:
        lookback_date = (datetime.now(timezone.utc) - timedelta(days=settings.LOOKBACK_DAYS)).isoformat()
        endpoint += f"&$filter=lastModifiedDateTime ge {lookback_date}"

    while endpoint:
        response_data = call_graph_api(endpoint, access_token)
        messages = response_data.get('value', [])
        all_messages.extend(messages)
        endpoint = response_data.get('@odata.nextLink')
    return all_messages

def main():
    try:
        access_token = get_access_token()
        logger.info("Successfully acquired access token.")

        target_teams = [settings.TEAMS_GROUP_NAME] if settings.TEAMS_GROUP_NAME else []

        if not target_teams:
            logger.info("No TEAMS_GROUP_NAME specified. Discovering all accessible teams...")
            try:
                teams = get_all_teams(access_token)
                target_teams = [t['displayName'] for t in teams]
                team_map = {t['displayName']: t['id'] for t in teams}
                logger.info(f"Discovered {len(target_teams)} teams: {', '.join(target_teams)}")
            except Exception as e:
                logger.error(f"Error discovering teams: {e}", exc_info=True)
                return
        else:
            team_map = {}

        for i, group_name in enumerate(target_teams):
            logger.info(f"[{i+1}/{len(target_teams)}] Processing Teams team: {group_name}...")
            try:
                team_id = team_map.get(group_name) or get_team_id_by_display_name(group_name, access_token)
                logger.info(f"  - Team ID: {team_id}")

                channels = get_channels_for_team(team_id, access_token)
                logger.info(f"  - Found {len(channels)} channels.")

                for channel in channels:
                    channel_id = channel['id']
                    channel_display_name = channel['displayName']
                    logger.info(f"    - Fetching messages from channel: {channel_display_name}")

                    messages = get_channel_messages(team_id, channel_id, access_token)
                    if messages:
                        logger.info(f"      - Found {len(messages)} message threads.")
                    for message in messages:
                        author_info = message.get('from', {})
                        author_name = "Unknown"
                        if author_info:
                            if author_info.get('user'):
                                author_name = author_info['user'].get('displayName', "Unknown User")
                            elif author_info.get('application'):
                                author_name = author_info['application'].get('displayName', "Unknown Application")
                        
                        extracted_data_schema = {
                            "id": f"teams-{message.get('id')}",
                            "source": "teams",
                            "source_id": message.get('id'),
                            "url": None, # Teams messages don't have a direct public URL like Jira/Confluence/SharePoint files
                            "title": message.get('subject') or f"Teams Message in {channel_display_name}",
                            "content": message.get('body', {}).get('content'),
                            "content_type": "message",
                            "created_at": message.get('createdDateTime'),
                            "updated_at": message.get('lastModifiedDateTime'),
                            "author": author_name,
                            "metadata": {
                                "teams_team_name": group_name,
                                "teams_team_id": team_id,
                                "teams_channel_name": channel_display_name,
                                "teams_channel_id": channel_id,
                                "message_type": message.get('messageType'),
                                "replies": []
                            }
                        }

                        # Process replies
                        replies = message.get('replies', [])
                        for reply in replies:
                            reply_author_info = reply.get('from', {})
                            reply_author_name = "Unknown"
                            if reply_author_info:
                                if reply_author_info.get('user'):
                                    reply_author_name = reply_author_info['user'].get('displayName', "Unknown User")
                                elif reply_author_info.get('application'):
                                    reply_author_name = reply_author_info['application'].get('displayName', "Unknown Application")
                            
                            extracted_data_schema["metadata"]["replies"].append({
                                "id": reply.get('id'),
                                "author": reply_author_name,
                                "created_at": reply.get('createdDateTime'),
                                "content": reply.get('body', {}).get('content')
                            })
                        
                        print(json.dumps(extracted_data_schema, ensure_ascii=False))
            except Exception as e:
                logger.error(f"Error processing Teams group '{group_name}': {e}", exc_info=True)

        # --- 일반 채팅 수집 ---
        if settings.TEAMS_CHAT_IDS:
            logger.info(f"Processing {len(settings.TEAMS_CHAT_IDS)} group chat(s)...")
            for i, chat_id in enumerate(settings.TEAMS_CHAT_IDS):
                try:
                    chat_info = get_chat_info(chat_id, access_token)
                    chat_topic = chat_info.get('topic') or f"Chat {chat_id[:8]}..."
                    logger.info(f"[{i+1}/{len(settings.TEAMS_CHAT_IDS)}] Processing chat: {chat_topic}")

                    messages = get_direct_chat_messages(chat_id, access_token)
                    logger.info(f"  - Found {len(messages)} messages.")

                    for message in messages:
                        # 시스템 메시지 제외 (이벤트, 멤버 추가 등)
                        if message.get('messageType') != 'message':
                            continue

                        author_info = message.get('from') or {}
                        if author_info.get('user'):
                            author_name = author_info['user'].get('displayName', 'Unknown User')
                        elif author_info.get('application'):
                            author_name = author_info['application'].get('displayName', 'Unknown Application')
                        else:
                            author_name = 'Unknown'

                        extracted_data_schema = {
                            "id": f"teams-chat-{message.get('id')}",
                            "source": "teams",
                            "source_id": message.get('id'),
                            "url": None,
                            "title": f"[{chat_topic}] {author_name}의 메시지",
                            "content": message.get('body', {}).get('content'),
                            "content_type": "chat_message",
                            "created_at": message.get('createdDateTime'),
                            "updated_at": message.get('lastModifiedDateTime'),
                            "author": author_name,
                            "metadata": {
                                "teams_chat_id": chat_id,
                                "teams_chat_topic": chat_topic,
                                "teams_chat_type": chat_info.get('chatType'),
                                "message_type": message.get('messageType'),
                            }
                        }
                        print(json.dumps(extracted_data_schema, ensure_ascii=False))

                except Exception as e:
                    logger.error(f"Error processing chat '{chat_id}': {e}", exc_info=True)

    except requests.exceptions.RequestException as e:
        logger.error(f"Error calling Microsoft Graph API: {e}")
        if e.response:
            logger.error(f"Response status: {e.response.status_code}")
            logger.debug(f"Response body: {e.response.text}")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)

if __name__ == "__main__":
    main()