"""
결과분석 모듈

- 답변 있음: 답변 내용 + 검색결과 + 첨부문서를 HTML로 저장 (LLM 재호출 없음)
- 답변 없음: LLM이 원인을 분석하여 HTML로 저장
설정의 analyze_no_answer 플래그가 ON일 때만 트리거됩니다.
"""

from company_llm_rag.history_store import save_analysis, set_analysis_pending
from company_llm_rag.retrieval_module import retrieve_documents
from company_llm_rag.llm.openai_provider import default_llm
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_ANALYSIS_PROMPT = """\
사용자가 다음 질문을 했지만 AI가 답변을 찾지 못했습니다.

[사용자 질문]
{question}

[검색된 관련 문서 {n}건 — 순위/소스/제목/관련도/내용]
{docs}

위 검색 결과를 바탕으로 아래 항목을 분석해 주세요.
반드시 HTML 형식으로만 작성하세요 (마크다운 사용 금지).
<h4>답변 불가 이유</h4>
<p>검색된 문서들이 질문에 답하기에 충분하지 않은 이유를 구체적으로 서술하세요.</p>
<h4>부족한 정보</h4>
<p>어떤 정보가 데이터베이스에 없거나 부족한지 서술하세요.</p>
<h4>개선 제안</h4>
<ul><li>향후 답변 품질 개선을 위해 추가 수집이 필요한 데이터</li></ul>
"""

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


def _build_docs_html(docs: list) -> str:
    if not docs:
        return '<p style="color:#888;font-size:0.85rem">검색된 문서 없음</p>'

    rows = []
    for i, doc in enumerate(docs, 1):
        meta = doc.get("metadata", {})
        source = meta.get("source", "?")
        title = meta.get("title") or meta.get("teams_channel_name") or "제목 없음"
        title_esc = title.replace("<", "&lt;").replace(">", "&gt;")
        rrf = doc.get("_rrf", 0)
        v_rank = doc.get("_vector_rank")
        k_rank = doc.get("_keyword_rank")

        v_cell = f'<span style="color:#0052cc">#{v_rank + 1}</span>' if v_rank is not None else '<span style="color:#ccc">—</span>'
        k_cell = f'<span style="color:#038387">#{k_rank + 1}</span>' if k_rank is not None else '<span style="color:#ccc">—</span>'

        pct = min(rrf / _RRF_MAX * 100, 100)
        bar_pct = int(pct)
        pct_color = "#2e7d32" if pct >= 60 else "#f57c00" if pct >= 30 else "#9e9e9e"
        bar = (
            f'<div style="display:flex;align-items:center;gap:6px">'
            f'<div style="width:80px;height:5px;background:#eee;border-radius:3px;flex-shrink:0">'
            f'<div style="width:{bar_pct}%;height:100%;background:{pct_color};border-radius:3px"></div>'
            f'</div>'
            f'<span style="font-size:0.78rem;font-weight:600;color:{pct_color};white-space:nowrap">{pct:.0f}%</span>'
            f'</div>'
        )
        bg = "#fffde7" if i <= 3 else ""
        rows.append(
            f'<tr style="background:{bg};border-bottom:1px solid #f0f0f0">'
            f'<td style="text-align:center;color:#999;font-size:0.78rem;padding:5px 4px">{i}</td>'
            f'<td style="padding:5px 4px">{_source_badge(source)}</td>'
            f'<td style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:5px 4px;max-width:320px" title="{title_esc}">{title_esc}</td>'
            f'<td style="padding:5px 8px;min-width:140px">{bar}</td>'
            f'<td style="text-align:center;font-size:0.78rem;padding:5px 4px">{v_cell}</td>'
            f'<td style="text-align:center;font-size:0.78rem;padding:5px 4px">{k_cell}</td>'
            f'</tr>'
        )

    th = 'style="padding:5px 4px;font-weight:600;font-size:0.73rem;color:#888;white-space:nowrap"'
    header = (
        f'<thead><tr style="background:#f7f7f7;border-bottom:2px solid #eee">'
        f'<th {th}>#</th><th {th}>소스</th><th {th}>제목</th>'
        f'<th {th}>관련도</th>'
        f'<th {th} title="벡터 검색 순위">벡터</th>'
        f'<th {th} title="키워드 검색 순위">키워드</th>'
        f'</tr></thead>'
    )
    return (
        '<div style="overflow-x:auto;border:1px solid #eee;border-radius:6px">'
        '<table style="width:100%;border-collapse:collapse;font-size:0.8rem">'
        f'{header}<tbody>{"".join(rows)}</tbody>'
        '</table></div>'
    )


async def analyze_no_answer(record_id: int, question: str) -> None:
    """
    백그라운드에서 답변없음 원인을 LLM으로 조사하고 DB에 저장합니다.
    결과는 HTML로 저장됩니다.
    """
    try:
        set_analysis_pending(record_id)
        logger.info(f"[NoAnswerAnalyzer] 조사 시작 record_id={record_id}")

        docs = retrieve_documents(question, n_results=15, return_scores=True)

        if docs:
            doc_lines = []
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
                doc_lines.append(
                    f"{i}. [{source}] {title} | RRF={rrf:.5f} | 매칭={match_info}\n   {preview}"
                )
            docs_text = "\n\n".join(doc_lines)
        else:
            docs_text = "관련 문서를 전혀 찾을 수 없습니다."

        prompt = _ANALYSIS_PROMPT.format(
            question=question,
            n=len(docs),
            docs=docs_text,
        )

        llm_html = default_llm.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )

        docs_html = _build_docs_html(docs)
        q_esc = question.replace("<", "&lt;").replace(">", "&gt;")

        toggle_btn = (
            f'<button onclick="var el=document.getElementById(\'ana-docs\');'
            f'var btn=this;'
            f'if(el.style.display===\'none\'){{el.style.display=\'\';btn.textContent=\'▲ 검색 결과 {len(docs)}건 접기\';}}'
            f'else{{el.style.display=\'none\';btn.textContent=\'▼ 검색 결과 {len(docs)}건 펼치기\';}}" '
            f'style="background:none;border:1px solid #ddd;border-radius:4px;padding:4px 10px;'
            f'font-size:0.75rem;color:#666;cursor:pointer;margin-top:14px;width:100%;text-align:left">'
            f'▼ 검색 결과 {len(docs)}건 펼치기'
            f'</button>'
        )

        html = (
            '<div style="font-family:inherit;font-size:0.85rem">'
            '<div style="margin-bottom:14px">'
            '<div style="font-size:0.72rem;color:#999;margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em">질문</div>'
            f'<div style="background:#f8f8f8;border-left:3px solid #6264a7;padding:8px 12px;border-radius:0 4px 4px 0">{q_esc}</div>'
            '</div>'
            '<div style="border-top:1px solid #eee;padding-top:14px">'
            '<div style="font-size:0.72rem;color:#999;margin-bottom:8px;text-transform:uppercase;letter-spacing:.04em">AI 분석</div>'
            f'<div style="line-height:1.75;color:#333">{llm_html}</div>'
            '</div>'
            f'{toggle_btn}'
            f'<div id="ana-docs" style="display:none;margin-top:8px">{docs_html}</div>'
            '</div>'
        )

        save_analysis(record_id, html, status="done")
        logger.info(f"[NoAnswerAnalyzer] 조사 완료 record_id={record_id}")

    except Exception as e:
        logger.error(f"[NoAnswerAnalyzer] 조사 실패 record_id={record_id}: {e}", exc_info=True)
        err_html = f'<p style="color:#c62828">조사 중 오류 발생: {e}</p>'
        save_analysis(record_id, err_html, status="error")


async def analyze_with_answer(record_id: int, question: str, answer: str, references: list) -> None:
    """
    답변이 있는 경우의 결과분석 — LLM 재호출 없이 답변·검색결과·첨부문서를 HTML로 저장합니다.
    """
    try:
        set_analysis_pending(record_id)
        docs = retrieve_documents(question, n_results=15, return_scores=True)

        q_esc = question.replace("<", "&lt;").replace(">", "&gt;")

        # 답변 HTML
        import re as _re
        answer_html = answer if answer.strip().startswith('<') else answer.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

        # 첨부문서 (SharePoint 파일 소스만)
        attachments = [r for r in references if r.get('source') == 'sharepoint' and r.get('content_type') == 'file']
        attachments_html = ''
        if attachments:
            items = []
            for a in attachments:
                title = a.get('title', '')
                url = a.get('url', '')
                label = title or url
                page_nums = a.get('page_nums', [])
                suffix = ''
                if page_nums:
                    if any(title.lower().endswith(e) for e in ('.pptx', '.ppt')):
                        suffix = f' <span style="color:#888;font-size:0.78rem">Slide {", ".join(str(n) for n in page_nums)}</span>'
                    else:
                        suffix = f' <span style="color:#888;font-size:0.78rem">p.{", ".join(str(n) for n in page_nums)}</span>'
                items.append(
                    f'<li style="margin-bottom:4px">'
                    f'<a href="{url}" target="_blank" style="color:#038387;text-decoration:none">{label}</a>'
                    f'{suffix}</li>'
                )
            attachments_html = (
                '<div style="margin-top:14px;border-top:1px solid #eee;padding-top:12px">'
                '<div style="font-size:0.72rem;color:#999;margin-bottom:6px;text-transform:uppercase;letter-spacing:.04em">첨부문서</div>'
                f'<ul style="margin:0;padding-left:18px;font-size:0.82rem">{"".join(items)}</ul>'
                '</div>'
            )

        # 검색결과 테이블
        docs_html = _build_docs_html(docs)
        toggle_btn = (
            f'<button onclick="var el=document.getElementById(\'ana-docs-{record_id}\');'
            f'var btn=this;'
            f'if(el.style.display===\'none\'){{el.style.display=\'\';btn.textContent=\'▲ 검색 결과 {len(docs)}건 접기\';}}'
            f'else{{el.style.display=\'none\';btn.textContent=\'▼ 검색 결과 {len(docs)}건 펼치기\';}}" '
            f'style="background:none;border:1px solid #ddd;border-radius:4px;padding:4px 10px;'
            f'font-size:0.75rem;color:#666;cursor:pointer;margin-top:14px;width:100%;text-align:left">'
            f'▼ 검색 결과 {len(docs)}건 펼치기'
            f'</button>'
        )

        html = (
            '<div style="font-family:inherit;font-size:0.85rem">'
            '<div style="margin-bottom:14px">'
            '<div style="font-size:0.72rem;color:#999;margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em">질문</div>'
            f'<div style="background:#f8f8f8;border-left:3px solid #6264a7;padding:8px 12px;border-radius:0 4px 4px 0">{q_esc}</div>'
            '</div>'
            '<div style="border-top:1px solid #eee;padding-top:14px">'
            '<div style="font-size:0.72rem;color:#999;margin-bottom:8px;text-transform:uppercase;letter-spacing:.04em">답변</div>'
            f'<div style="line-height:1.75;color:#333">{answer_html}</div>'
            '</div>'
            f'{attachments_html}'
            f'{toggle_btn}'
            f'<div id="ana-docs-{record_id}" style="display:none;margin-top:8px">{docs_html}</div>'
            '</div>'
        )

        save_analysis(record_id, html, status="done")
        logger.info(f"[Analyzer] 결과분석 저장 record_id={record_id}")

    except Exception as e:
        logger.error(f"[Analyzer] 결과분석 실패 record_id={record_id}: {e}", exc_info=True)
        err_html = f'<p style="color:#c62828">분석 중 오류 발생: {e}</p>'
        save_analysis(record_id, err_html, status="error")
