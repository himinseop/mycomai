"""
HTML 콘텐츠 정제 유틸리티

데이터 추출기들이 공통으로 사용하는 HTML → plain text 변환 함수를 제공합니다.
"""

import re
from bs4 import BeautifulSoup

from company_llm_rag.logger import get_logger

logger = get_logger(__name__)


def parse_confluence_storage_format(html_content: str) -> str:
    """
    Confluence Storage Format(HTML 기반)을 plain text로 변환합니다.

    Confluence의 body.storage.value는 HTML 유사 XML 포맷입니다.
    BeautifulSoup으로 태그를 제거하고 가독성 있는 텍스트를 추출합니다.

    Args:
        html_content: Confluence Storage Format 문자열

    Returns:
        태그가 제거된 plain text
    """
    if not html_content or not isinstance(html_content, str):
        return ""

    try:
        # lxml 파서 우선 사용 (더 빠르고 관대함), 없으면 html.parser로 fallback
        try:
            soup = BeautifulSoup(html_content, "lxml")
        except Exception:
            soup = BeautifulSoup(html_content, "html.parser")

        # Confluence 매크로 블록 처리 (code, panel, info, warning 등)
        # ac:plain-text-body 태그의 내용은 보존
        for macro_body in soup.find_all("ac:plain-text-body"):
            macro_body.replace_with(f"\n{macro_body.get_text()}\n")

        # ac:parameter, ac:structured-macro 등 Confluence 전용 태그 제거 (내용 유지)
        for tag in soup.find_all(re.compile(r"^ac:")):
            tag.unwrap()

        # ri:* 태그 제거 (Confluence 리소스 식별자, 내용 없음)
        for tag in soup.find_all(re.compile(r"^ri:")):
            tag.decompose()

        # 줄바꿈 보존: 블록 레벨 태그 앞뒤에 개행 추가
        for tag in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6",
                                   "li", "tr", "br", "div", "section"]):
            tag.insert_before("\n")
            tag.insert_after("\n")

        # 테이블 셀(td, th)은 탭으로 구분
        for tag in soup.find_all(["td", "th"]):
            tag.insert_after("\t")

        text = soup.get_text(separator=" ")

        # 연속된 공백/개행 정리
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    except Exception as e:
        logger.warning(f"Failed to parse Confluence Storage Format HTML: {e}. Returning raw content.")
        return html_content


def parse_teams_html(html_content: str) -> str:
    """
    Microsoft Teams 메시지 HTML을 plain text로 변환합니다.

    Teams Graph API의 body.content는 일반 HTML 포맷입니다.
    BeautifulSoup으로 태그를 제거하고 가독성 있는 텍스트를 추출합니다.

    Args:
        html_content: Teams 메시지 HTML 문자열

    Returns:
        태그가 제거된 plain text
    """
    if not html_content or not isinstance(html_content, str):
        return ""

    # Teams가 plain text 메시지를 그대로 반환하는 경우 HTML 태그가 없을 수 있음
    # 간단한 체크: '<' 문자가 없으면 이미 plain text
    if "<" not in html_content:
        return html_content.strip()

    try:
        try:
            soup = BeautifulSoup(html_content, "lxml")
        except Exception:
            soup = BeautifulSoup(html_content, "html.parser")

        # Teams 첨부파일/카드 등 불필요한 요소 제거
        for tag in soup.find_all(["attachment", "img"]):
            tag.decompose()

        # 줄바꿈 보존: 블록 레벨 태그 앞뒤에 개행 추가
        for tag in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6",
                                   "li", "tr", "br", "div", "section"]):
            tag.insert_before("\n")
            tag.insert_after("\n")

        # 테이블 셀은 탭으로 구분
        for tag in soup.find_all(["td", "th"]):
            tag.insert_after("\t")

        text = soup.get_text(separator=" ")

        # 연속된 공백/개행 정리
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    except Exception as e:
        logger.warning(f"Failed to parse Teams HTML: {e}. Returning raw content.")
        return html_content
