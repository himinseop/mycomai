import json
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger
from company_llm_rag.data_extraction.m365.auth import get_access_token, call_graph_api
from company_llm_rag.data_extraction.html_utils import parse_teams_html

logger = get_logger(__name__)

_PROGRESS_EVERY = 50

def _fmt_elapsed(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))

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

        if not settings.TEAMS_GROUP_NAMES:
            logger.info("TEAMS_GROUP_NAME이 설정되지 않아 Teams 채널 수집을 건너뜁니다.")
            return

        target_teams = settings.TEAMS_GROUP_NAMES
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
                    total_msgs = len(messages)
                    if total_msgs:
                        logger.info(f"[Teams][{group_name}][{channel_display_name}] {total_msgs}개 메시지 발견. 수집 시작...")
                    ch_start = time.time()
                    ch_count = 0
                    for message in messages:
                        if message.get('messageType') != 'message':
                            continue

                        author_info = message.get('from', {})
                        author_name = "Unknown"
                        if author_info:
                            if author_info.get('user'):
                                author_name = author_info['user'].get('displayName', "Unknown User")
                            elif author_info.get('application'):
                                author_name = author_info['application'].get('displayName', "Unknown Application")

                        # 본문
                        body_text = parse_teams_html(message.get('body', {}).get('content', ""))
                        if len(body_text.strip()) < 50:
                            continue

                        # replies: content에 합치기
                        reply_blocks = []
                        for reply in message.get('replies', []):
                            if reply.get('messageType') != 'message':
                                continue
                            reply_body = parse_teams_html(reply.get('body', {}).get('content', ""))
                            if not reply_body.strip():
                                continue
                            reply_author_info = reply.get('from', {})
                            if reply_author_info.get('user'):
                                reply_author = reply_author_info['user'].get('displayName', 'Unknown')
                            elif reply_author_info.get('application'):
                                reply_author = reply_author_info['application'].get('displayName', 'Unknown')
                            else:
                                reply_author = 'Unknown'
                            reply_date = (reply.get('createdDateTime') or '')[:10]
                            reply_blocks.append(f"[Reply by {reply_author} on {reply_date}]\n{reply_body.strip()}")

                        content = body_text
                        if reply_blocks:
                            content = body_text + '\n\n' + '\n\n'.join(reply_blocks)

                        extracted_data_schema = {
                            "id": f"teams-{message.get('id')}",
                            "source": "teams",
                            "source_id": message.get('id'),
                            "url": None,
                            "title": message.get('subject') or f"Teams Message in {channel_display_name}",
                            "content": content,
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
                                "reply_count": len(reply_blocks),
                            }
                        }

                        print(json.dumps(extracted_data_schema, ensure_ascii=False))
                        ch_count += 1
                        if ch_count % _PROGRESS_EVERY == 0:
                            pct = int(ch_count / total_msgs * 100) if total_msgs else 0
                            elapsed = _fmt_elapsed(time.time() - ch_start)
                            logger.info(f"[Teams][{group_name}][{channel_display_name}] {ch_count}/{total_msgs} ({pct}%) | 경과: {elapsed}")
                    if total_msgs:
                        logger.info(f"[Teams][{group_name}][{channel_display_name}] 완료: {ch_count}개 | 소요: {_fmt_elapsed(time.time() - ch_start)}")
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
                    total_msgs = len(messages)
                    logger.info(f"[Teams][{chat_topic}] {total_msgs}개 메시지 발견. 수집 시작...")
                    chat_start = time.time()
                    chat_count = 0

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

                        chat_body_text = parse_teams_html(message.get('body', {}).get('content', ""))
                        if len(chat_body_text.strip()) < 50:
                            continue

                        extracted_data_schema = {
                            "id": f"teams-chat-{message.get('id')}",
                            "source": "teams",
                            "source_id": message.get('id'),
                            "url": None,
                            "title": f"[{chat_topic}] {author_name}의 메시지",
                            "content": chat_body_text,
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
                        chat_count += 1
                        if chat_count % _PROGRESS_EVERY == 0:
                            pct = int(chat_count / total_msgs * 100) if total_msgs else 0
                            elapsed = _fmt_elapsed(time.time() - chat_start)
                            logger.info(f"[Teams][{chat_topic}] {chat_count}/{total_msgs} ({pct}%) | 경과: {elapsed}")
                    logger.info(f"[Teams][{chat_topic}] 완료: {chat_count}개 | 소요: {_fmt_elapsed(time.time() - chat_start)}")

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