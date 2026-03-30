import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

import requests

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger
from company_llm_rag.data_extraction.m365.auth import get_access_token, call_graph_api
from company_llm_rag.data_extraction.html_utils import parse_teams_html

logger = get_logger(__name__)

_PROGRESS_EVERY = 50


_IMAGES_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'static', 'images')


def _download_graph_image(url: str, access_token: str) -> Optional[str]:
    """Graph API 이미지를 로컬에 다운로드하고 /static/images/ 경로를 반환합니다."""
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '')
        ext = '.png'
        if 'jpeg' in content_type or 'jpg' in content_type:
            ext = '.jpg'
        elif 'gif' in content_type:
            ext = '.gif'
        filename = hashlib.md5(url.encode()).hexdigest() + ext
        os.makedirs(_IMAGES_DIR, exist_ok=True)
        filepath = os.path.join(_IMAGES_DIR, filename)
        with open(filepath, 'wb') as f:
            f.write(resp.content)
        return f"/static/images/{filename}"
    except Exception as e:
        logger.warning(f"이미지 다운로드 실패: {e}")
        return None


def _parse_reply_html_with_images(html_content: str, access_token: str) -> str:
    """Reply HTML을 텍스트로 변환하며, 이미지를 원래 위치에 마크다운으로 삽입합니다."""
    from bs4 import BeautifulSoup, NavigableString
    import re

    if not html_content:
        return ""

    try:
        try:
            soup = BeautifulSoup(html_content, "lxml")
        except Exception:
            soup = BeautifulSoup(html_content, "html.parser")

        # <img> → 마크다운 이미지로 치환 (원래 위치 유지)
        for img_tag in soup.find_all("img"):
            src = img_tag.get("src", "")
            if "graph.microsoft.com" in src:
                local_path = _download_graph_image(src, access_token)
                if local_path:
                    img_tag.replace_with(NavigableString(f"\n![참고 이미지]({local_path})\n"))
                    continue
            img_tag.decompose()

        for tag in soup.find_all(["attachment"]):
            tag.decompose()
        for tag in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6",
                                   "li", "tr", "br", "div", "section"]):
            tag.insert_before("\n")
            tag.insert_after("\n")
        for tag in soup.find_all(["td", "th"]):
            tag.insert_after("\t")

        text = soup.get_text(separator=" ")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    except Exception as e:
        logger.warning(f"Reply HTML 파싱 실패: {e}")
        return html_content


def _extract_hub_question(body_text: str) -> str:
    """Knowledge Hub 본문에서 [질문] 섹션의 텍스트만 추출합니다."""
    marker = '**[질문]**'
    idx = body_text.find(marker)
    if idx < 0:
        return body_text  # 마커 없으면 전체 반환
    after = body_text[idx + len(marker):].strip()
    # 다음 섹션 시작([대화 맥락 요약], [오사장 답변] 등) 전까지 추출
    for end_marker in ['**[대화 맥락 요약]**', '**[오사장 답변]**', '**[', '[Reply by ']:
        end_idx = after.find(end_marker)
        if end_idx >= 0:
            after = after[:end_idx]
            break
    return after.strip()


def _extract_adaptive_card_text(attachments: List[Dict]) -> str:
    """Adaptive Card 첨부파일에서 TextBlock 텍스트를 추출합니다."""
    texts = []
    for att in attachments:
        if att.get('contentType') != 'application/vnd.microsoft.card.adaptive':
            continue
        try:
            card = json.loads(att.get('content', '{}'))
        except (json.JSONDecodeError, TypeError):
            continue
        for block in card.get('body', []):
            if block.get('type') == 'TextBlock':
                text = (block.get('text') or '').strip()
                if text:
                    texts.append(text)
    return '\n'.join(texts)

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

        target_teams = list(settings.TEAMS_GROUP_NAMES)
        if settings.KNOWLEDGE_HUB_TEAM_NAME:
            hub = settings.KNOWLEDGE_HUB_TEAM_NAME
            if hub not in target_teams:
                target_teams.append(hub)

        if not target_teams:
            logger.info("TEAMS_GROUP_NAME과 KNOWLEDGE_HUB_TEAM_NAME이 모두 설정되지 않아 Teams 채널 수집을 건너뜁니다.")
            return
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
                    is_hub = (settings.KNOWLEDGE_HUB_TEAM_NAME
                              and group_name == settings.KNOWLEDGE_HUB_TEAM_NAME)
                    for message in messages:
                        if message.get('messageType') != 'message':
                            continue

                        # Knowledge Hub: 답변(reply)이 있는 메시지만 수집
                        if is_hub and not message.get('replies'):
                            continue

                        author_info = message.get('from', {})
                        author_name = "Unknown"
                        if author_info:
                            if author_info.get('user'):
                                author_name = author_info['user'].get('displayName', "Unknown User")
                            elif author_info.get('application'):
                                author_name = author_info['application'].get('displayName', "Unknown Application")

                        # 본문 (Adaptive Card인 경우 카드 텍스트 추출)
                        body_text = parse_teams_html(message.get('body', {}).get('content', ""))
                        if not body_text.strip():
                            body_text = _extract_adaptive_card_text(message.get('attachments', []))
                        if not is_hub and len(body_text.strip()) < 50:
                            continue

                        # replies: content에 합치기
                        reply_blocks = []
                        for reply in message.get('replies', []):
                            if reply.get('messageType') != 'message':
                                continue
                            # Knowledge Hub: 이미지를 원래 위치에 마크다운으로 삽입
                            if is_hub:
                                reply_body = _parse_reply_html_with_images(
                                    reply.get('body', {}).get('content', ""), access_token)
                            else:
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

                        msg_id = message.get('id')
                        channel_url = f"https://teams.microsoft.com/l/message/{channel_id}/{msg_id}" if channel_id and msg_id else None

                        metadata = {
                            "teams_team_name": group_name,
                            "teams_team_id": team_id,
                            "teams_channel_name": channel_display_name,
                            "teams_channel_id": channel_id,
                            "message_type": message.get('messageType'),
                            "reply_count": len(reply_blocks),
                        }

                        if is_hub:
                            # Knowledge Hub: 질문만 임베딩, 답변 원문은 별도 저장
                            content = _extract_hub_question(body_text)
                            hub_reply = '\n\n'.join(
                                block.split('\n', 1)[1].strip() if '\n' in block else block
                                for block in reply_blocks
                            )
                            metadata["is_hub_direct"] = True
                            metadata["hub_reply_content"] = hub_reply
                        else:
                            content = body_text
                            if reply_blocks:
                                content = body_text + '\n\n' + '\n\n'.join(reply_blocks)

                        extracted_data_schema = {
                            "id": f"teams-{msg_id}",
                            "source": "teams",
                            "source_id": msg_id,
                            "url": channel_url,
                            "title": message.get('subject') or f"Teams Message in {channel_display_name}",
                            "content": content,
                            "content_type": "message",
                            "created_at": message.get('createdDateTime'),
                            "updated_at": message.get('lastModifiedDateTime'),
                            "author": author_name,
                            "metadata": metadata,
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

                        chat_msg_id = message.get('id')
                        chat_url = f"https://teams.microsoft.com/l/message/{chat_id}/{chat_msg_id}" if chat_id and chat_msg_id else None

                        extracted_data_schema = {
                            "id": f"teams-chat-{chat_msg_id}",
                            "source": "teams",
                            "source_id": chat_msg_id,
                            "url": chat_url,
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