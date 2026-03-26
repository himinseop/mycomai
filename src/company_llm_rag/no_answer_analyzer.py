"""
결과분석 모듈

- 답변 있음: 답변 내용 + 검색결과 + 첨부문서를 HTML로 저장 (LLM 재호출 없음)
- 답변 없음: LLM이 원인을 분석하여 HTML로 저장
설정의 analyze_no_answer 플래그가 ON일 때만 트리거됩니다.
"""

import asyncio
import functools
from typing import Optional

from company_llm_rag.history_store import (
    save_analysis, set_analysis_pending,
    save_group_analysis, set_group_analysis_pending,
    get_session_detail,
)
from company_llm_rag.retrieval_module import retrieve_documents
from company_llm_rag.llm.openai_provider import default_llm
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_ANALYSIS_PROMPT_NO_ANSWER = """\
당신은 AI 검색 시스템의 품질 분석가입니다.
사용자가 아래 질문을 했지만 AI가 답변을 찾지 못했고, 사용자가 불만족 피드백을 남겼습니다.

[질문 그룹 — 총 {turn_count}개 질문]
{transcript}

[재검색 결과 {n}건 — 순위/소스/제목/관련도/내용]
{docs}

[원본 답변에 제공된 참고문서]
{original_refs}

[원본 답변 vs 재검색 비교]
{comparison}

다음 순서로 분석 작업을 수행하고 결과를 HTML로 작성하세요 (마크다운 사용 금지).

<h4>1. 질문 분석</h4>
<p>질문의 핵심 의도, 키워드, 요구하는 정보 유형을 분석합니다.</p>
<h4>2. 검색 결과 검토</h4>
<p>재검색된 {n}건의 문서와 원본 답변 참고문서를 비교합니다. 원본에서 누락된 관련 문서가 있는지, 원본 참고문서의 관련도가 적절했는지 분석합니다.</p>
<h4>3. 답변 불가 원인</h4>
<p>AI가 답변하지 못한 구체적인 이유를 서술합니다. (정보 부재, 검색 정확도 문제, 질문 모호성 등)</p>
<h4>4. 부족한 정보</h4>
<p>데이터베이스에 없거나 부족한 정보가 무엇인지 구체적으로 서술합니다.</p>
<h4>5. 개선 제안</h4>
<ul><li>답변 품질 개선을 위해 추가 수집이 필요한 데이터 또는 시스템 개선 방안을 제안합니다.</li></ul>
"""

_ANALYSIS_PROMPT_WITH_ANSWER = """\
당신은 AI 검색 시스템의 품질 분석가입니다.
사용자가 아래 질문 그룹에 대한 답변을 받았지만 피드백을 남겼습니다.

[질문 그룹 — 총 {turn_count}개 질문]
{transcript}

[재검색 결과 {n}건 — 순위/소스/제목/관련도/내용]
{docs}

[원본 답변에 제공된 참고문서]
{original_refs}

[원본 답변 vs 재검색 비교]
{comparison}

다음 순서로 분석 작업을 수행하고 결과를 HTML로 작성하세요 (마크다운 사용 금지).

<h4>1. 질문 분석</h4>
<p>질문의 핵심 의도, 키워드, 사용자가 기대했을 정보를 분석합니다.</p>
<h4>2. 답변 적절성 검토</h4>
<p>제공된 답변이 질문에 충분히 답했는지, 부정확하거나 누락된 내용은 없는지 검토합니다.</p>
<h4>3. 검색 결과 분석</h4>
<p>재검색된 {n}건의 문서와 원본 답변 참고문서를 비교합니다. 원본에서 누락된 관련도 높은 문서가 있는지, 원본 참고문서의 관련도가 적절했는지 분석합니다.</p>
{dissatisfaction_section}<h4>{improve_num}. 개선 제안</h4>
<ul><li>답변 품질 및 검색 정확도 개선을 위한 구체적인 방안을 제안합니다.</li></ul>
"""

# 하위 호환: 기존 코드에서 _ANALYSIS_PROMPT 참조 시 no_answer 프롬프트 사용
_ANALYSIS_PROMPT = _ANALYSIS_PROMPT_NO_ANSWER

_SOURCE_COLORS = {
    "jira": "#0052cc",
    "confluence": "#0065ff",
    "sharepoint": "#038387",
    "teams": "#6264a7",
    "local": "#555",
}


def _source_badge(source: str) -> str:
    color = _SOURCE_COLORS.get(source, "#888")
    return (
        f'<span style="display:inline-block;padding:1px 6px;border-radius:3px;'
        f'font-size:0.72rem;font-weight:600;color:#fff;background:{color};'
        f'margin-right:4px">{source}</span>'
    )


_RRF_MAX = 2 / 61  # rank=0 벡터+키워드 동시 1위일 때 최대값 ≈ 0.03279


def _build_ref_link_html(meta: dict, title_esc: str, url: str) -> str:
    """참고문서 링크를 소스별 계층 형식 HTML로 반환합니다."""
    from urllib.parse import urlparse
    source = meta.get("source", "")
    light = "color:#aaa;font-size:0.78rem;text-decoration:none"
    main_style = "color:#0052cc;text-decoration:none"
    sep = '<span style="color:#ddd;margin:0 3px">/</span>'

    def a(href, text, style=None):
        s = style or main_style
        return f'<a href="{href}" target="_blank" style="{s}">{text}</a>'

    if source == "jira":
        project_key = meta.get("jira_project_key") or (
            meta.get("jira_issue_key", "").split("-")[0] if meta.get("jira_issue_key") else ""
        )
        issue_key = meta.get("jira_issue_key", "")
        try:
            parsed = urlparse(url)
            project_url = (
                f"{parsed.scheme}://{parsed.netloc}/jira/software/projects/{project_key}/boards"
                if project_key else ""
            )
        except Exception:
            project_url = ""
        proj_part = (
            a(project_url, project_key, light) if project_url
            else (f'<span style="{light}">{project_key}</span>' if project_key else "")
        )
        # 제목이 이미 이슈 키로 시작하면 중복 제거
        clean_title_esc = title_esc
        key_prefix = f"[{issue_key}]"
        if issue_key and clean_title_esc.startswith(key_prefix):
            clean_title_esc = clean_title_esc[len(key_prefix):].strip()
        issue_text = f"[{issue_key}] {clean_title_esc}" if issue_key else clean_title_esc
        doc_link = a(url, issue_text) if url else f'<span style="{main_style}">{issue_text}</span>'
        return f'{proj_part}{sep}{doc_link}' if proj_part else doc_link

    elif source == "confluence":
        space_key = meta.get("confluence_space_key", "")
        space_name = meta.get("confluence_space_name") or space_key
        try:
            parsed = urlparse(url)
            space_url = (
                f"{parsed.scheme}://{parsed.netloc}/wiki/spaces/{space_key}/overview"
                if space_key else ""
            )
        except Exception:
            space_url = ""
        space_part = (
            a(space_url, space_name, light) if space_url and space_name
            else (f'<span style="{light}">{space_name}</span>' if space_name else "")
        )
        doc_link = a(url, title_esc) if url else f'<span>{title_esc}</span>'
        return f'{space_part}{sep}{doc_link}' if space_part else doc_link

    elif source == "sharepoint":
        site_name = meta.get("sharepoint_site_name", "")
        file_path = meta.get("sharepoint_file_path", "")
        if "/root:" in file_path:
            file_path = file_path.split("/root:", 1)[1].lstrip("/")
        try:
            parsed = urlparse(url)
            segs = [s for s in parsed.path.split("/") if s]
            site_url = (
                f"{parsed.scheme}://{parsed.netloc}/sites/{segs[1]}"
                if len(segs) >= 2 and segs[0].lower() == "sites"
                else f"{parsed.scheme}://{parsed.netloc}"
            )
        except Exception:
            site_url = ""
        if file_path:
            parts = file_path.split("/")
            doc_name_esc = parts[-1].replace("<", "&lt;").replace(">", "&gt;")
            folder_raw = "/".join(parts[:-1])
            folder_esc = folder_raw.replace("<", "&lt;").replace(">", "&gt;")
            folder_url = f"{site_url}/{folder_raw}" if folder_raw and site_url else ""
        else:
            doc_name_esc = title_esc
            folder_esc = ""
            folder_url = ""
        try:
            site_display = site_name or urlparse(site_url).netloc
        except Exception:
            site_display = site_name
        site_part = (
            a(site_url, site_display.replace("<", "&lt;").replace(">", "&gt;"), light)
            if site_url and site_display else ""
        )
        folder_part = (
            a(folder_url, folder_esc, light) if folder_url and folder_esc
            else (f'<span style="{light}">{folder_esc}</span>' if folder_esc else "")
        )
        doc_link = a(url, doc_name_esc) if url else f'<span>{doc_name_esc}</span>'
        parts_html = [p for p in [site_part, folder_part, doc_link] if p]
        return sep.join(parts_html) if parts_html else doc_link

    elif source == "teams":
        team_name = meta.get("teams_team_name") or ""
        channel_name = meta.get("teams_channel_name") or ""
        chat_topic = meta.get("teams_chat_topic") or ""
        if team_name in ("None",): team_name = ""
        if channel_name in ("None",): channel_name = ""
        if chat_topic in ("None", "null"): chat_topic = ""
        author = meta.get("author") or ""
        created_at = meta.get("created_at") or ""
        channel_label = channel_name or chat_topic
        location = (
            f"{team_name}/{channel_label}" if team_name and channel_label
            else team_name or channel_label
        )
        date_str = ""
        if created_at:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(created_at.rstrip("Z"))
                date_str = dt.strftime("%Y.%m.%d %H:%M")
            except Exception:
                date_str = created_at[:16]
        snippet = (meta.get("content") or "")[:60].replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")
        author_prefix = f"[{author}]: " if author else ""
        msg_text = f"{author_prefix}{snippet}" if snippet else title_esc
        loc_span = (
            f'<span style="{light}">{location.replace("<", "&lt;").replace(">", "&gt;")}</span>'
            if location else ""
        )
        msg_link = a(url, msg_text) if url else f'<span>{msg_text}</span>'
        date_span = (
            f'<span style="color:#aaa;font-size:0.72rem;margin-left:6px">{date_str}</span>'
            if date_str else ""
        )
        parts_html = [p for p in [loc_span, msg_link] if p]
        return sep.join(parts_html) + date_span

    # Default
    return a(url, title_esc) if url else f'<span>{title_esc}</span>'


def _build_docs_html(docs: list, ref_urls: set = None) -> str:
    """검색 결과를 HTML 테이블로 변환합니다.
    ref_urls: 실제 답변에 참고문서로 제공된 URL 집합. 해당 행은 강조 표시합니다.
    """
    if not docs:
        return '<p style="color:#888;font-size:0.85rem">검색된 문서 없음</p>'

    ref_urls = ref_urls or set()

    rows = []
    for i, doc in enumerate(docs, 1):
        meta = doc.get("metadata", {})
        source = meta.get("source", "?")
        title = meta.get("title") or meta.get("teams_channel_name") or "제목 없음"
        title_esc = title.replace("<", "&lt;").replace(">", "&gt;")
        rrf = doc.get("_rrf", 0)
        v_rank = doc.get("_vector_rank")
        k_rank = doc.get("_keyword_rank")

        is_injected = doc.get("_injected", False)
        v_cell = f'<span style="color:#0052cc">#{v_rank + 1}</span>' if v_rank is not None else '<span style="color:#ccc">—</span>'
        k_cell = f'<span style="color:#038387">#{k_rank + 1}</span>' if k_rank is not None else '<span style="color:#ccc">—</span>'
        d_cell = (
            '<span style="display:inline-block;padding:1px 5px;border-radius:3px;'
            'font-size:0.68rem;font-weight:600;color:#fff;background:#6264a7">직접조회</span>'
            if is_injected else '<span style="color:#ccc">—</span>'
        )

        if is_injected:
            bar = (
                '<div style="display:flex;align-items:center;gap:6px">'
                '<div style="width:80px;height:5px;background:#eee;border-radius:3px;flex-shrink:0">'
                '<div style="width:100%;height:100%;background:#6264a7;border-radius:3px"></div>'
                '</div>'
                '<span style="font-size:0.78rem;font-weight:600;color:#6264a7;white-space:nowrap">직접조회</span>'
                '</div>'
            )
        else:
            pct = min(rrf / _RRF_MAX * 100, 100)
            bar_pct = int(pct)
            pct_color = "#2e7d32" if pct >= 80 else "#f57c00" if pct >= 10 else "#9e9e9e"
            bar = (
                f'<div style="display:flex;align-items:center;gap:6px">'
                f'<div style="width:80px;height:5px;background:#eee;border-radius:3px;flex-shrink:0">'
                f'<div style="width:{bar_pct}%;height:100%;background:{pct_color};border-radius:3px"></div>'
                f'</div>'
                f'<span style="font-size:0.78rem;font-weight:600;color:{pct_color};white-space:nowrap">{pct:.0f}%</span>'
                f'</div>'
            )
        url = meta.get("url") or ""
        if not url and source == "teams":
            from company_llm_rag.rag_system import _build_teams_url
            url = _build_teams_url(meta)

        title_cell = _build_ref_link_html(meta, title_esc, url)

        is_ref = url in ref_urls
        if is_ref:
            bg = "#e8f5e9"  # 연초록 — 참고문서로 제공된 항목
            ref_badge = '<span style="display:inline-block;margin-left:6px;padding:1px 5px;border-radius:3px;font-size:0.68rem;font-weight:600;color:#fff;background:#2e7d32;vertical-align:middle">참고문서</span>'
        else:
            bg = "#fffde7" if i <= 3 else ""
            ref_badge = ""

        rows.append(
            f'<tr style="background:{bg};border-bottom:1px solid #f0f0f0">'
            f'<td style="text-align:center;color:#999;font-size:0.78rem;padding:5px 4px">{i}</td>'
            f'<td style="padding:5px 4px">{_source_badge(source)}</td>'
            f'<td style="padding:5px 4px;max-width:300px;word-break:break-word;line-height:1.5">{title_cell}{ref_badge}</td>'
            f'<td style="padding:5px 8px;min-width:140px">{bar}</td>'
            f'<td style="text-align:center;font-size:0.78rem;padding:5px 4px">{v_cell}</td>'
            f'<td style="text-align:center;font-size:0.78rem;padding:5px 4px">{k_cell}</td>'
            f'<td style="text-align:center;padding:5px 4px">{d_cell}</td>'
            f'</tr>'
        )

    th = 'style="padding:5px 4px;font-weight:600;font-size:0.73rem;color:#888;white-space:nowrap"'
    header = (
        f'<thead><tr style="background:#f7f7f7;border-bottom:2px solid #eee">'
        f'<th {th}>#</th><th {th}>소스</th><th {th}>제목</th>'
        f'<th {th}>관련도</th>'
        f'<th {th} title="벡터 검색 순위">벡터</th>'
        f'<th {th} title="키워드 검색 순위">키워드</th>'
        f'<th {th} title="이슈 키 직접 조회">직접조회</th>'
        f'</tr></thead>'
    )
    return (
        '<div style="overflow-x:auto;border:1px solid #eee;border-radius:6px">'
        '<table style="width:100%;border-collapse:collapse;font-size:0.8rem">'
        f'{header}<tbody>{"".join(rows)}</tbody>'
        '</table></div>'
    )


def _build_docs_text(docs: list) -> str:
    """LLM 프롬프트용 문서 목록 텍스트를 구성합니다."""
    if not docs:
        return "관련 문서를 전혀 찾을 수 없습니다."
    lines = []
    for i, doc in enumerate(docs, 1):
        meta = doc.get("metadata", {})
        source = meta.get("source", "?")
        title = meta.get("title") or "제목 없음"
        rrf = doc.get("_rrf", 0)
        v_rank = doc.get("_vector_rank")
        k_rank = doc.get("_keyword_rank")
        preview = (doc.get("content") or "")[:200].replace("\n", " ")
        v_str = f"벡터#{v_rank+1}" if v_rank is not None else ""
        k_str = f"키워드#{k_rank+1}" if k_rank is not None else ""
        match_info = ", ".join(filter(None, [v_str, k_str])) or "없음"
        lines.append(
            f"{i}. [{source}] {title} | RRF={rrf:.5f} | 매칭={match_info}\n   {preview}"
        )
    return "\n\n".join(lines)


async def analyze_bad_feedback(
    record_id: int,
    question: str,
    answer: str,
    is_no_answer: bool,
    conversation_history=None,
    session_id: Optional[str] = None,
    group_feedback: int = 0,
) -> None:
    """
    👎 피드백을 받은 대화에 대해 LLM 분석을 수행하고 DB에 저장합니다.
    질문을 재검색한 후, 답변 유무에 따라 적합한 프롬프트로 분석합니다.
    session_id가 제공되면 그룹 전체 transcript를 대상으로 분석합니다.
    """
    try:
        if session_id:
            set_group_analysis_pending(session_id)
        else:
            set_analysis_pending(record_id)
        logger.info(f"[Analyzer] 분석 시작 record_id={record_id} no_answer={is_no_answer} session_id={session_id}")

        # 그룹 전체 턴 수집
        all_turns_data = []
        all_questions = [question]

        if session_id:
            group = get_session_detail(session_id)
            if group and group.get("turns"):
                all_turns_data = group["turns"]
                all_questions = [t["question"] for t in all_turns_data]
                # 마지막 턴으로 단건 데이터 갱신
                last = all_turns_data[-1]
                question = last["question"]
                answer = last["answer"] or ""
                is_no_answer = bool(last.get("is_no_answer", False))

        # 모든 질문을 합쳐서 검색
        combined_query = " ".join(all_questions)
        docs = await asyncio.to_thread(
            retrieve_documents, combined_query, n_results=15, return_scores=True
        )

        # 원본 참고문서 URL 수집
        original_ref_urls: set = set()
        if all_turns_data:
            for t in all_turns_data:
                for r in t.get("references", []):
                    u = r.get("url", "")
                    if u:
                        original_ref_urls.add(u)

        # 원본 참고문서 텍스트
        if original_ref_urls:
            original_refs_text = "\n".join(
                f"- {url}" for url in sorted(original_ref_urls)
            )
        else:
            original_refs_text = "원본 답변에 참고문서 없음"

        # 재검색 결과 vs 원본 비교
        discovery_docs = []  # 원본에 미포함이지만 관련도 높은 문서
        in_original = []     # 원본에 포함되고 재검색에서도 상위인 문서
        missed_in_original = []  # 재검색 상위이지만 원본에 미포함

        _RRF_THRESHOLD = _RRF_MAX * 0.10  # 관련도 10% 이상

        for doc in docs:
            meta = doc.get("metadata", {})
            url = meta.get("url", "") or ""
            rrf = doc.get("_rrf", 0)
            dist = doc.get("_distance", 1.0)
            title = meta.get("title", "") or "제목 없음"
            source = meta.get("source", "?")

            if rrf < _RRF_THRESHOLD:
                continue

            pct = min(rrf / _RRF_MAX * 100, 100)
            entry = f"[{source}] {title} (관련도:{pct:.0f}%)"

            if url and url in original_ref_urls:
                in_original.append(entry)
            elif dist <= 0.35:
                missed_in_original.append(entry)
                discovery_docs.append(doc)

        # 비교 텍스트 구성
        comparison_lines = []
        if in_original:
            comparison_lines.append(f"재검색에서도 상위이고 원본에도 포함된 문서 ({len(in_original)}건):")
            for e in in_original:
                comparison_lines.append(f"  ✓ {e}")
        if missed_in_original:
            comparison_lines.append(f"\n원본 답변에서 누락된 관련도 높은 문서 ({len(missed_in_original)}건):")
            for e in missed_in_original:
                comparison_lines.append(f"  ✗ {e}")
        if not in_original and not missed_in_original:
            comparison_lines.append("재검색 결과 중 관련도 높은 문서 없음")

        comparison_text = "\n".join(comparison_lines)

        docs_text = _build_docs_text(docs)

        # 전체 대화 transcript 구성
        if all_turns_data:
            transcript_parts = []
            for t in all_turns_data:
                q_part = f"Q{t['turn_index']}: {t['question']}"
                a_part = f"A{t['turn_index']}: {(t['answer'] or '')[:500]}"
                transcript_parts.append(f"{q_part}\n{a_part}")
            transcript = "\n\n".join(transcript_parts)
            turn_count = len(all_turns_data)
        elif conversation_history and len(conversation_history) >= 2:
            # 하위 호환: conversation_history 직접 전달 방식
            pairs = []
            i = 0
            while i < len(conversation_history) - 1:
                if (conversation_history[i]["role"] == "user"
                        and conversation_history[i + 1]["role"] == "assistant"):
                    pairs.append(
                        f"Q{len(pairs)+1}: {conversation_history[i]['content']}\n"
                        f"A{len(pairs)+1}: {(conversation_history[i+1]['content'] or '')[:500]}"
                    )
                    i += 2
                else:
                    i += 1
            if pairs:
                transcript = "\n\n".join(pairs)
                turn_count = len(pairs)
            else:
                transcript = f"Q1: {question}\nA1: {(answer or '')[:500]}"
                turn_count = 1
        else:
            transcript = f"Q1: {question}\nA1: {(answer or '')[:500]}"
            turn_count = 1

        if is_no_answer:
            prompt = _ANALYSIS_PROMPT_NO_ANSWER.format(
                transcript=transcript, turn_count=turn_count, n=len(docs), docs=docs_text,
                original_refs=original_refs_text, comparison=comparison_text,
            )
        else:
            if group_feedback == -1:
                dissatisfaction_section = (
                    "<h4>4. 불만족 원인 추정</h4>\n"
                    "<p>사용자가 불만족한 이유를 구체적으로 추정합니다. "
                    "(답변 부정확, 정보 부족, 관련 없는 내용 포함 등)</p>\n"
                )
                improve_num = 5
            else:
                dissatisfaction_section = ""
                improve_num = 4
            prompt = _ANALYSIS_PROMPT_WITH_ANSWER.format(
                transcript=transcript,
                turn_count=turn_count,
                n=len(docs),
                docs=docs_text,
                dissatisfaction_section=dissatisfaction_section,
                improve_num=improve_num,
                original_refs=original_refs_text,
                comparison=comparison_text,
            )

        llm_html = await asyncio.to_thread(
            functools.partial(
                default_llm.chat,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
        )

        html = (
            '<div style="font-family:inherit;font-size:0.85rem">'
            '<div style="font-size:0.72rem;color:#999;margin-bottom:8px;text-transform:uppercase;letter-spacing:.04em">분석 작업 보고서</div>'
            f'<div style="line-height:1.75;color:#333">{llm_html}</div>'
            '</div>'
        )
        if discovery_docs:
            discovery_html = _build_docs_html(discovery_docs, original_ref_urls)
            html += (
                f'<div style="margin-top:14px;padding-top:14px;border-top:1px solid #eee">'
                f'<div style="font-size:0.72rem;color:#e65100;margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em">'
                f'발견된 관련 문서 {len(discovery_docs)}건 (원본 답변에서 누락)</div>'
                f'{discovery_html}</div>'
            )

        if session_id:
            save_group_analysis(session_id, html, status="done")
        else:
            save_analysis(record_id, html, status="done")
        logger.info(f"[Analyzer] 분석 완료 record_id={record_id} docs={len(docs)}")

    except Exception as e:
        logger.error(f"[Analyzer] 분석 실패 record_id={record_id}: {e}", exc_info=True)
        err_html = f'<p style="color:#c62828">분석 중 오류 발생: {e}</p>'
        if session_id:
            save_group_analysis(session_id, err_html, status="error")
        else:
            save_analysis(record_id, err_html, status="error")


# 하위 호환: 기존 코드에서 참조할 수 있는 함수 (deprecated → analyze_bad_feedback 사용 권장)
async def analyze_no_answer(record_id: int, question: str) -> None:
    await analyze_bad_feedback(record_id, question, "", True)
