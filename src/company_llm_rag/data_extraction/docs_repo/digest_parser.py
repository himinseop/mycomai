"""
다이제스트 헤더 파서 (#61 11-A)

platform/sharepoint-index/digests/*.md 의 blockquote 헤더 블록을 파싱합니다.
파일럿 검증(voucher)을 기준으로 한 계약:

    > **이 문서는 YYYY년 M월 시점의 기획이다. …**
    >
    > - 원본: [파일명](SharePoint URL)
    > - 문서 기록 날짜: YYYY-MM-DD · 종류: 기획서 · 버전: ver0.4
    > - SharePoint 위치: `/...`
    > - 관련 주제: [voucher](../../features/voucher.md), ...
    > - 관련 일감: 구현 WMPO-123(사유) · 후속 WPLUS-45(사유) · 원인 KEY · 미구현(비고)

파서는 관대하게 동작한다 — 필드 하나가 깨져 있어도 경고만 남기고 나머지는 계속 처리한다.
"""

import json
import re
from typing import Dict, List, Tuple

from company_llm_rag.sp_guid import extract_sp_guid

# 관련 일감 역할 어휘 4종 고정 (설계 11-A). 그 외 어휘는 무시 + 경고.
_ROLE_WORDS = {"구현", "후속", "원인", "미구현"}

# 닫는 괄호 누락 오타 허용 — ')' 또는 공백 전까지를 URL로 취급
_ORIGIN_URL_RE = re.compile(r"\((https?://[^)\s]+)")
# 일 단위 없는 YYYY-MM 표기 허용
_DATE_RE = re.compile(r"(\d{4}-\d{2}(?:-\d{2})?)")
_KIND_RE = re.compile(r"종류:\s*([^·]+?)\s*(?:·|$)")
_VERSION_RE = re.compile(r"버전:\s*([^\s·]+)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_HREF_CATEGORY_RE = re.compile(r"(features|sites)/([^/]+?)\.md$")
_ROLE_HEAD_RE = re.compile(r"^([가-힣]+)")
_ISSUE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9]*-\d+)")
_NOTE_RE = re.compile(r"\((.*)\)\s*$")


def _normalize_href(href: str) -> str:
    """../../features/voucher.md → voucher, ../../sites/admin.md → site:admin"""
    m = _HREF_CATEGORY_RE.search(href.strip())
    if not m:
        return ""
    category, slug = m.group(1), m.group(2)
    return slug if category == "features" else f"site:{slug}"


def _parse_topics(rest: str, relpath: str, warnings: List[str]) -> List[str]:
    """'관련 주제:' 뒤의 값을 슬러그 리스트로 정규화합니다 (평문/마크다운 링크 두 형식 모두 처리)."""
    rest = rest.strip()
    if not rest or rest.startswith("없음"):
        return []

    links = _LINK_RE.findall(rest)
    topics: List[str] = []
    if links:
        # 마크다운 링크가 있으면 href를 기준으로 정규화 (평문 슬러그가 앞에 중복돼 있어도 무시)
        for _text, href in links:
            norm = _normalize_href(href)
            if not norm:
                warnings.append(f"[{relpath}] 관련 주제 링크 경로 인식 실패: {href!r}")
                continue
            if norm not in topics:
                topics.append(norm)
        return topics

    # 평문 슬러그만 있는 경우 (마크다운 링크 없음)
    plain_part = rest.split("→")[0]
    for tok in re.split(r"[,·]", plain_part):
        tok = tok.strip()
        if tok and tok not in topics:
            topics.append(tok)
    return topics


def _parse_issue_entry(entry: str) -> Tuple[str, str, str]:
    """'구현 WMPO-1597(출시 범위 구현; 스펙아웃 제외)' → (role, key, note)"""
    m_role = _ROLE_HEAD_RE.match(entry)
    if not m_role:
        return "", "", ""
    role = m_role.group(1)
    remainder = entry[len(role):].strip()

    key = ""
    m_key = _ISSUE_KEY_RE.match(remainder)
    if m_key:
        key = m_key.group(1)
        remainder = remainder[len(key):].strip()

    note = ""
    m_note = _NOTE_RE.search(remainder)
    if m_note:
        note = m_note.group(1).strip()

    return role, key, note


def _parse_issues(rest: str, relpath: str, warnings: List[str]) -> Tuple[List[Dict], bool]:
    """'관련 일감:' 뒤의 값을 [{role,key,note}, ...]로 파싱합니다. 4종 외 어휘는 경고 후 무시."""
    issues: List[Dict] = []
    not_implemented = False
    for raw_entry in rest.split("·"):
        entry = raw_entry.strip()
        if not entry:
            continue
        role, key, note = _parse_issue_entry(entry)
        if not role:
            warnings.append(f"[{relpath}] 관련 일감 항목 파싱 실패: {entry!r}")
            continue
        if role not in _ROLE_WORDS:
            warnings.append(f"[{relpath}] 알 수 없는 관련 일감 역할 무시: {role!r} (항목: {entry!r})")
            continue
        if role == "미구현":
            not_implemented = True
        issues.append({"role": role, "key": key, "note": note})
    return issues, not_implemented


def _empty_metadata() -> Dict:
    return {
        "docs_category": "digest",
        "digest_date": "",
        "digest_kind": "",
        "digest_version": "",
        "sp_guid": "",
        "digest_topics": "",
        "digest_issues": "[]",
        "not_implemented": False,
    }


def parse_digest(content: str, relpath: str) -> Dict:
    """
    다이제스트 마크다운을 파싱합니다.

    Returns:
        {"content": 헤더 URL 줄 제거된 본문, "metadata": {...}, "url": 원본 SharePoint URL, "warnings": [...]}
    """
    warnings: List[str] = []
    lines = content.splitlines()

    start = next((i for i, l in enumerate(lines) if l.lstrip().startswith(">")), None)
    if start is None:
        warnings.append("헤더 블록쿼트를 찾을 수 없음 — 헤더 필드 없이 수집")
        return {"content": content, "metadata": _empty_metadata(), "url": "", "warnings": warnings}

    end = start
    while end < len(lines) and lines[end].lstrip().startswith(">"):
        end += 1
    header_lines = lines[start:end]

    sp_url = ""
    sp_guid = ""
    digest_date = ""
    digest_kind = ""
    digest_version = ""
    topics: List[str] = []
    issues: List[Dict] = []
    not_implemented = False
    has_topics_line = False
    remove_offsets: set = set()

    for idx, hline in enumerate(header_lines):
        stripped = hline.lstrip(">").strip()

        if stripped.startswith("- 원본:"):
            m = _ORIGIN_URL_RE.search(stripped)
            if m:
                sp_url = m.group(1)
                sp_guid = extract_sp_guid(sp_url)
            else:
                warnings.append(f"[{relpath}] 원본 URL 파싱 실패: {stripped!r}")
            remove_offsets.add(idx)

        elif stripped.startswith("- 문서 기록 날짜:"):
            m_date = _DATE_RE.search(stripped)
            m_kind = _KIND_RE.search(stripped)
            m_ver = _VERSION_RE.search(stripped)
            digest_date = m_date.group(1) if m_date else ""
            digest_kind = m_kind.group(1).strip() if m_kind else ""
            digest_version = m_ver.group(1).strip() if m_ver else ""
            # '없음'/'확인 불가'는 의도된 무날짜 표기 — 경고 없이 빈 날짜로 처리
            if not digest_date and not re.search(r"날짜:\s*(없음|확인 불가)", stripped):
                warnings.append(f"[{relpath}] 문서 기록 날짜 파싱 실패: {stripped!r}")
            if not digest_kind:
                warnings.append(f"[{relpath}] 종류 파싱 실패: {stripped!r}")
            if not digest_version:
                warnings.append(f"[{relpath}] 버전 파싱 실패: {stripped!r}")

        elif stripped.startswith("- SharePoint 위치:"):
            remove_offsets.add(idx)

        elif stripped.startswith("- 관련 주제:"):
            has_topics_line = True
            rest = stripped[len("- 관련 주제:"):]
            topics = _parse_topics(rest, relpath, warnings)

        elif stripped.startswith("- 관련 일감:"):
            rest = stripped[len("- 관련 일감:"):]
            issues, not_implemented = _parse_issues(rest, relpath, warnings)

        # 그 외 (시점 경고 볼드 문장, 빈 blockquote 줄)는 본문에 그대로 유지

    # 관련 주제는 대다수(약 98%) 문서에 있는 필드라 없으면 "주제 미지정"으로 경고한다.
    # 관련 일감은 반대로 대다수(97%)에 없는 선택 필드라 부재 자체는 경고하지 않는다.
    if not has_topics_line:
        warnings.append(f"[{relpath}] 관련 주제 라인 없음 (주제 미지정 추정)")

    cleaned_lines = [
        l for i, l in enumerate(lines)
        if not (start <= i < end and (i - start) in remove_offsets)
    ]
    cleaned_content = "\n".join(cleaned_lines)

    metadata = {
        "docs_category": "digest",
        "digest_date": digest_date,
        "digest_kind": digest_kind,
        "digest_version": digest_version,
        "sp_guid": sp_guid,
        "digest_topics": ",".join(topics),
        "digest_issues": json.dumps(issues, ensure_ascii=False),
        "not_implemented": not_implemented,
    }
    return {"content": cleaned_content, "metadata": metadata, "url": sp_url, "warnings": warnings}
