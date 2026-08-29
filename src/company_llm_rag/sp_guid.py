"""
SharePoint 문서 GUID 추출 (#61 11-A)

다이제스트 헤더의 원본 링크와 검색 시 sharepoint 문서 URL 양쪽에서
동일한 방식으로 sourcedoc GUID를 추출해야 원본 다운랭크 매칭이 성립한다.
"""

import re
from urllib.parse import unquote

_SOURCEDOC_RE = re.compile(r"sourcedoc=([^&]+)", re.IGNORECASE)
_GUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")


def extract_sp_guid(url: str) -> str:
    """SharePoint URL에서 sourcedoc GUID를 대문자로 추출합니다. 경로형 URL 등 없으면 빈 문자열."""
    if not url:
        return ""
    m = _SOURCEDOC_RE.search(url)
    if not m:
        return ""
    decoded = unquote(m.group(1))
    g = _GUID_RE.search(decoded)
    return g.group(0).upper() if g else ""
