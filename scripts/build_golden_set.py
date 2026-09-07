"""골든 평가셋 초안 생성기 (#63 1단계)

플랫폼매뉴얼(platform/features, platform/sites)을 절(heading) 단위로 쪼개 LLM에게
현장 직원이 물을 법한 질문 + 근거 문장(verbatim) + 핵심 사실을 만들게 하고,
Hub 답변(hub_replies, is_active=1), 실사용 질문(chat_history), 거부(abstain) 케이스를
더해 tests/eval/golden_draft.jsonl에 씁니다(전부 reviewed: false).

이어서 자동 품질 필터를 적용해 생존 케이스만 tests/eval/golden.jsonl에 씁니다.
사람 검토가 끝나면 golden.jsonl에서 각 줄의 reviewed를 true로 바꿔주세요.

설계: docs/issues/63/design.md §2.1

사용법:
  python scripts/build_golden_set.py                    # 전체 생성 (Docker 안에서)
  python scripts/build_golden_set.py --dry-run           # LLM 호출 없이 절 목록만 출력
  python scripts/build_golden_set.py --limit-files 3     # 매뉴얼 파일 수 제한(비용 절감/테스트용)

Docker 실행 예 (CLAUDE.md 참고):
  docker run --rm -v "$PWD/src:/app" -v "$PWD/tests:/app/tests" -v "$PWD/scripts:/app/scripts" \\
    -v "$PWD/db:/app/db" -v "/Users/we/Dev/o2olab/docs:/app/docs_repo:ro" \\
    -w /app -e PYTHONPATH=/app --env-file .env mycomai-rag \\
    python scripts/build_golden_set.py
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from company_llm_rag.config import settings  # noqa: E402

_EVAL_DIR = Path(__file__).resolve().parent.parent / "tests" / "eval"
_DRAFT_PATH = _EVAL_DIR / "golden_draft.jsonl"
_GOLDEN_PATH = _EVAL_DIR / "golden.jsonl"

_SUBDIRS = ["platform/features", "platform/sites"]  # 다이제스트(platform/sharepoint-index/digests)는 제외
_MIN_SECTION_CHARS = 100
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.*)$")
_NUMBERING_RE = re.compile(r"^\d+(\.\d+)*\.?\s*")

# 모든 매뉴얼이 공유하는 표준 템플릿의 부록성 절("관련 일감·기획서" 계열: 일감 링크·기획서
# 계보·출처 다이제스트 목록 — 서술형 사실이 아니라 링크 표라서 질문 생성 자원 낭비).
# "확인이 필요한 항목"만은 예외 — 매뉴얼 저자가 스스로 "이 매뉴얼에 없다"고 표시한
# 항목 표라서 abstain 케이스의 최적 소스로 별도 취급한다.
_APPENDIX_SUBSTRINGS = ("계보", "관련 일감", "다이제스트", "직접 연결", "출처·기획 이력", "구현 일감 연결")
_ABSTAIN_SOURCE_HEADING = "확인이 필요한 항목"


def _strip_numbering(heading: str) -> str:
    return _NUMBERING_RE.sub("", heading).strip()


def classify_heading(heading: str) -> str:
    """'manual' | 'abstain_source' | 'appendix' — 절 제목으로 용도를 분류."""
    if _strip_numbering(heading) == _ABSTAIN_SOURCE_HEADING:
        return "abstain_source"
    if any(s in heading for s in _APPENDIX_SUBSTRINGS):
        return "appendix"
    return "manual"

_DRAFT_MODEL = "gpt-4o-mini"
_DRAFT_TEMPERATURE = 0.3

_TARGET_LIVE = 15
_TARGET_ABSTAIN = 8
_MIN_QUESTION_CHARS = 8


# ── 매뉴얼 파일 탐색 · 절 분할 ───────────────────────────────────────────

def find_manual_files(docs_root: Path, limit_files: Optional[int] = None) -> List[Dict]:
    """platform/features, platform/sites 하위 마크다운 파일 목록(README 제외)."""
    files = []
    for subdir in _SUBDIRS:
        base = docs_root / subdir
        if not base.is_dir():
            print(f"[경고] 디렉토리 없음 — 건너뜀: {base}")
            continue
        category = Path(subdir).name
        for p in sorted(base.glob("*.md")):
            if p.name.lower() == "readme.md":
                continue
            relpath = f"{subdir}/{p.name}"
            files.append({"path": p, "relpath": relpath, "category": category, "doc_id": f"docs-{relpath}"})
    files.sort(key=lambda f: f["relpath"])
    if limit_files:
        files = files[:limit_files]
    return files


def extract_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return fallback


def split_sections(content: str, file_title: str) -> List[Dict]:
    """## / ### 헤딩 경계로 절을 분할합니다. 헤딩 이전 내용은 '개요' 절로 묶습니다."""
    lines = content.splitlines()
    sections = []
    current_heading = f"{file_title} — 개요"
    current_lines: List[str] = []

    for line in lines:
        m = _HEADING_RE.match(line.rstrip())
        if m:
            text = "\n".join(current_lines).strip()
            if text:
                sections.append({"heading": current_heading, "content": text})
            current_heading = m.group(2).strip()
            current_lines = []
        else:
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                continue  # H1 제목 줄은 본문에서 제외 (파일 제목으로 별도 사용)
            current_lines.append(line)

    text = "\n".join(current_lines).strip()
    if text:
        sections.append({"heading": current_heading, "content": text})
    return [s for s in sections if len(s["content"]) >= _MIN_SECTION_CHARS]


def build_candidates(files: List[Dict]) -> (Dict[str, List[Dict]], Dict[str, Dict]):
    """파일별 절 후보를 (매뉴얼 QA 후보, abstain 소스 절)로 분리해 반환합니다.

    매뉴얼 QA 후보는 표준 템플릿의 부록성 절("관련 일감·기획서" 계열)을 제외하고,
    content 길이 내림차순으로 정렬합니다(알찬 절 우선). abstain 소스는 파일당 최대
    하나("확인이 필요한 항목" 절, 여러 개면 이어붙임).
    """
    by_file: Dict[str, List[Dict]] = {}
    abstain_by_file: Dict[str, Dict] = {}
    for f in files:
        try:
            content = f["path"].read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"[경고] 파일 읽기 실패 — 건너뜀: {f['path']} ({e})")
            continue
        title = extract_title(content, f["path"].stem)
        sections = split_sections(content, title)

        cands = []
        abstain_parts = []
        for s in sections:
            kind = classify_heading(s["heading"])
            if kind == "manual":
                cands.append({**f, "file_title": title, "heading": s["heading"], "content": s["content"]})
            elif kind == "abstain_source":
                abstain_parts.append(s["content"])
            # "appendix"는 두 풀 어디에도 포함하지 않음 (링크 표 — 서술형 사실 없음)

        cands.sort(key=lambda c: -len(c["content"]))
        if cands:
            by_file[f["relpath"]] = cands
        if abstain_parts:
            abstain_by_file[f["relpath"]] = {
                **f, "file_title": title, "content": "\n\n".join(abstain_parts),
            }
    return by_file, abstain_by_file


def select_sections(by_file: Dict[str, List[Dict]], target_n: int) -> List[Dict]:
    """모든 파일에 최소 1개씩 골고루 배분하며(round-robin), 알찬 절부터 target_n개 선택."""
    queues = {k: list(v) for k, v in by_file.items()}
    file_keys = list(queues.keys())
    selected: List[Dict] = []
    idx = 0
    guard = 0
    while len(selected) < target_n and any(queues.values()) and guard < 100000:
        guard += 1
        key = file_keys[idx % len(file_keys)]
        idx += 1
        if queues[key]:
            selected.append(queues[key].pop(0))
    return selected


# ── LLM 호출 ────────────────────────────────────────────────────────────

def _extract_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


_MANUAL_PROMPT = """당신은 사내 플랫폼매뉴얼을 검토해 RAG 챗봇 평가용 골든 질문을 만드는 담당자입니다.
아래는 매뉴얼 "{file_title}"의 한 절("{heading}")입니다.

[본문]
{content}

이 본문 내용만 근거로, 현장 직원(가맹점 사장님 CS 담당·운영팀)이 실제로 물어볼 법한
한국어 질문을 1~2개 만드세요. 각 질문에 대해:
- question: 자연스러운 한국어 질문
- expected_facts: 그 질문의 답이 되는 핵심 사실 2~4개 (본문에 명시된 내용만, 각 항목은 짧은 한 문장)
- source_excerpt: expected_facts의 근거가 되는 본문 문장을 "원문 그대로" 발췌 (글자를 바꾸거나 요약하지 말고 있는 문장을 그대로 복사)

본문에 실제로 없는 내용은 절대 만들지 마세요. 서술형 사실이 없는 절(표/목록만 있고
질문 만들기 부적절)이면 questions를 빈 배열로 반환하세요.

다른 설명 없이 JSON만 출력하세요:
{{"questions": [{{"question": "...", "expected_facts": ["...", "..."], "source_excerpt": "..."}}]}}
"""

_ABSTAIN_TABLE_PROMPT = """아래는 사내 플랫폼매뉴얼 "{file_title}"에서 저자가 스스로
"아직 확인되지 않았다"고 표시해 둔 항목 목록입니다(매뉴얼 본문에는 답이 없습니다).

[확인이 필요한 항목]
{content}

이 중 현장 직원(가맹점 사장님 CS 담당·운영팀)이 실제로 물어볼 법한 항목을 하나 골라,
자연스러운 한국어 질문 하나로 바꿔주세요. 목록에 있는 내용 그대로 질문화하고, 답을
지어내지 마세요.

다른 설명 없이 JSON만 출력하세요:
{{"question": "...", "notes": "어느 항목을 근거로 만들었는지 한 줄"}}
"""

_ABSTAIN_GAP_PROMPT = """당신은 사내 플랫폼매뉴얼 "{file_title}"의 내용을 검토해, 이 매뉴얼이 다루지 않는
정책/규칙에 대한 질문을 만드는 담당자입니다.

[매뉴얼 본문 발췌]
{content}

입력 포맷 규칙, 자릿수·글자수 제한, 대소문자 구분, 예외 상황 처리 기준 등 현장에서
물어볼 법하지만 이 발췌에는 명시적으로 나와있지 않은 세부 규정 질문을 1개 한국어로
만드세요. 그런 질문을 찾을 수 없으면 question을 null로 반환하세요.

다른 설명 없이 JSON만 출력하세요:
{{"question": "...", "notes": "이 발췌에 없다고 판단한 이유 한 줄"}}
"""

_HUB_FACTS_PROMPT = """아래는 사내 Q&A 답변입니다. 이 답변에 담긴 핵심 사실을 2~4개,
짧은 한국어 문장으로 뽑아주세요. 답변에 없는 내용은 만들지 마세요.

[질문] {question}
[답변] {reply}

다른 설명 없이 JSON만 출력하세요: {{"facts": ["...", "..."]}}
"""


def generate_manual_questions(llm, section: Dict) -> List[Dict]:
    prompt = _MANUAL_PROMPT.format(
        file_title=section["file_title"], heading=section["heading"], content=section["content"][:3000]
    )
    raw = llm.chat([{"role": "user", "content": prompt}], temperature=_DRAFT_TEMPERATURE, max_tokens=800)
    parsed = _extract_json(raw)
    if parsed is None or not isinstance(parsed.get("questions"), list):
        print(f"  [경고] JSON 파싱 실패 — 절 건너뜀: {section['relpath']} / {section['heading']}")
        return []
    return parsed["questions"]


def generate_abstain_from_table(llm, file_title: str, content: str) -> Optional[Dict]:
    """'확인이 필요한 항목' 절(저자가 스스로 미확인 표시한 표)에서 질문 하나를 뽑는다 — 우선 경로."""
    prompt = _ABSTAIN_TABLE_PROMPT.format(file_title=file_title, content=content[:4000])
    raw = llm.chat([{"role": "user", "content": prompt}], temperature=_DRAFT_TEMPERATURE, max_tokens=300)
    parsed = _extract_json(raw)
    if parsed is None:
        return None
    q = parsed.get("question")
    if not q or not isinstance(q, str):
        return None
    return {"question": q.strip(), "notes": parsed.get("notes", "")}


def generate_abstain_gap_question(llm, file_title: str, content: str) -> Optional[Dict]:
    """'확인이 필요한 항목' 절이 없는 파일용 폴백 — 본문에서 LLM이 직접 빈틈을 찾는다."""
    prompt = _ABSTAIN_GAP_PROMPT.format(file_title=file_title, content=content[:4000])
    raw = llm.chat([{"role": "user", "content": prompt}], temperature=_DRAFT_TEMPERATURE, max_tokens=300)
    parsed = _extract_json(raw)
    if parsed is None:
        return None
    q = parsed.get("question")
    if not q or not isinstance(q, str):
        return None
    return {"question": q.strip(), "notes": parsed.get("notes", "")}


def generate_hub_facts(llm, question: str, reply: str) -> List[str]:
    prompt = _HUB_FACTS_PROMPT.format(question=question, reply=reply[:2000])
    raw = llm.chat([{"role": "user", "content": prompt}], temperature=_DRAFT_TEMPERATURE, max_tokens=400)
    parsed = _extract_json(raw)
    if parsed is None or not isinstance(parsed.get("facts"), list):
        return []
    return [f for f in parsed["facts"] if isinstance(f, str) and f.strip()]


# ── 각 소스별 케이스 생성 ────────────────────────────────────────────────

def build_manual_cases(llm, selected_sections: List[Dict]) -> (List[Dict], Dict[str, str]):
    """returns (cases, {case_id: section_content}) — section_content는 필터 단계 substring 검사용."""
    cases = []
    section_content_by_id = {}
    seq_by_file: Dict[str, int] = {}

    for section in selected_sections:
        print(f"  [매뉴얼] {section['relpath']} / {section['heading'][:40]}")
        questions = generate_manual_questions(llm, section)
        stem = Path(section["path"]).stem
        for q in questions:
            question = (q.get("question") or "").strip()
            excerpt = (q.get("source_excerpt") or "").strip()
            facts = [f for f in (q.get("expected_facts") or []) if isinstance(f, str) and f.strip()]
            if not question:
                continue
            seq_by_file[stem] = seq_by_file.get(stem, 0) + 1
            cid = f"man-{stem}-{seq_by_file[stem]:03d}"
            cases.append({
                "id": cid,
                "question": question,
                "category": "manual",
                "expected_facts": facts,
                "expected_sources": [section["doc_id"]],
                "must_abstain": False,
                "source_excerpt": excerpt,
                "notes": f"{section['relpath']} / {section['heading']}",
                "reviewed": False,
            })
            section_content_by_id[cid] = section["content"]
    return cases, section_content_by_id


def build_abstain_cases(
    llm, abstain_by_file: Dict[str, Dict], by_file: Dict[str, List[Dict]], target_n: int
) -> List[Dict]:
    """abstain 케이스 생성 — '확인이 필요한 항목' 절이 있는 파일을 우선 사용(고품질·저비용).

    해당 절이 없는 파일은 폴백으로 LLM이 본문에서 직접 빈틈을 찾는다(비용은 더 들고
    품질도 덜 보장됨 — design.md §2.1의 방식).
    """
    cases = []
    attempted = 0

    table_keys = sorted(abstain_by_file.keys())
    for key in table_keys:
        if len(cases) >= target_n:
            break
        entry = abstain_by_file[key]
        print(f"  [거부 후보 — 확인이 필요한 항목] {key}")
        attempted += 1
        result = generate_abstain_from_table(llm, entry["file_title"], entry["content"])
        if result is None:
            continue
        cid = f"abstain-{Path(key).stem}-{len(cases) + 1:03d}"
        cases.append({
            "id": cid,
            "question": result["question"],
            "category": "abstain",
            "expected_facts": [],
            "expected_sources": [],
            "must_abstain": True,
            "source_excerpt": "",
            "notes": f"{entry['doc_id']} '확인이 필요한 항목' 절 기준 — {result.get('notes', '')}",
            "reviewed": False,
        })

    # 폴백: 목표에 못 미치면 '확인이 필요한 항목' 절이 없는 파일에서 LLM이 직접 빈틈을 찾는다.
    if len(cases) < target_n:
        fallback_keys = sorted(k for k in by_file if k not in abstain_by_file)
        for key in fallback_keys:
            if len(cases) >= target_n:
                break
            sections = by_file[key][:2]
            content = "\n\n".join(s["content"] for s in sections)
            title = sections[0]["file_title"]
            doc_id = sections[0]["doc_id"]
            print(f"  [거부 후보 — 폴백] {key}")
            attempted += 1
            result = generate_abstain_gap_question(llm, title, content)
            if result is None:
                continue
            cid = f"abstain-{Path(key).stem}-{len(cases) + 1:03d}"
            cases.append({
                "id": cid,
                "question": result["question"],
                "category": "abstain",
                "expected_facts": [],
                "expected_sources": [],
                "must_abstain": True,
                "source_excerpt": "",
                "notes": f"{doc_id} 기준 미커버(폴백) — {result.get('notes', '')}",
                "reviewed": False,
            })

    print(f"  [거부] {attempted}개 파일 시도 → {len(cases)}건 생성")
    return cases


def build_hub_cases(llm, db_path: str) -> List[Dict]:
    cases = []
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT doc_id, question, reply_content FROM hub_replies WHERE is_active=1 ORDER BY id"
        ).fetchall()
        con.close()
    except sqlite3.Error as e:
        print(f"[경고] hub_replies 조회 실패 — hub 케이스 건너뜀: {e}")
        return cases

    for i, row in enumerate(rows, start=1):
        question = (row["question"] or "").strip()
        reply = (row["reply_content"] or "").strip()
        if not question or not reply:
            continue
        print(f"  [Hub] {question[:40]}")
        facts = generate_hub_facts(llm, question, reply)
        cases.append({
            "id": f"hub-{i:03d}",
            "question": question,
            "category": "hub",
            "expected_facts": facts,
            "expected_sources": [],
            "must_abstain": False,
            "source_excerpt": reply[:300],
            "notes": f"hub_replies.doc_id={row['doc_id']}",
            "reviewed": False,
        })
    return cases


def build_live_cases(db_path: str, target_n: int) -> List[Dict]:
    cases = []
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT DISTINCT question FROM chat_history "
            "WHERE is_no_answer=0 AND length(question) > 8 ORDER BY id DESC LIMIT 300"
        ).fetchall()
        con.close()
    except sqlite3.Error as e:
        print(f"[경고] chat_history 조회 실패 — live 케이스 건너뜀: {e}")
        return cases

    seen_norm = set()
    for row in rows:
        if len(cases) >= target_n:
            break
        q = (row["question"] or "").strip()
        if q.startswith("http") or "sess-" in q:
            continue
        norm = _normalize_question(q)
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        cases.append({
            "id": f"live-{len(cases) + 1:03d}",
            "question": q,
            "category": "live",
            "expected_facts": [],
            "expected_sources": [],
            "must_abstain": False,
            "source_excerpt": "",
            "notes": "실사용 질문 — 정답은 사람이 검토 시 기입 (공란이면 faithfulness만 채점)",
            "reviewed": False,
        })
    return cases


# ── 정규화 · 품질 필터 ───────────────────────────────────────────────────

def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _normalize_question(q: str) -> str:
    q = _normalize_ws(q).lower()
    return re.sub(r"[?!.]+$", "", q).strip()


# 인용 참조 괄호: 본문 문장 끝에 흔한 "([2.3](#23-...))" 형태 — 절 번호로 가는 셀프
# 링크. LLM이 문장을 발췌하며 이런 참조를 자연스럽게 생략하는 경우가 많아
# (사실 왜곡이 아님) 매칭 검사에서는 통째로 제거한다.
_CITATION_REF_RE = re.compile(r"\(\s*(?:\[[^\]]+\]\([^)]+\)[,\s]*)+\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_SYNTAX_CHARS_RE = re.compile(r"[|*`_>#]")
_NON_WORD_RE = re.compile(r"[^\w가-힣]+")


def _normalize_for_match(s: str) -> str:
    """source_excerpt가 실제로 본문에서 나온 것인지 판정하기 위한 정규화.

    표 구분자(|)·굵게(**)·인용 참조 괄호·마크다운 링크·문장부호 차이는 LLM이
    "원문 그대로" 발췌하면서도 흔히 정리하는 서식이라(사실 왜곡이 아님) 전부
    제거하고 단어만 비교한다 — 그래야 실제 지어낸 사실(허구 단어)만 걸러진다.
    """
    s = s or ""
    s = _CITATION_REF_RE.sub(" ", s)
    s = _MD_LINK_RE.sub(r"\1", s)
    s = _MD_SYNTAX_CHARS_RE.sub(" ", s)
    s = _NON_WORD_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def apply_quality_filter(cases: List[Dict], section_content_by_id: Dict[str, str]) -> List[Dict]:
    """자동 품질 필터: source_excerpt 검증(manual), 최소 길이, 질문 중복 제거."""
    survivors = []
    seen_questions = set()
    dropped = {"excerpt": 0, "short": 0, "dup": 0}

    for case in cases:
        if len(case["question"].strip()) < _MIN_QUESTION_CHARS:
            dropped["short"] += 1
            continue

        if case["category"] == "manual" and case.get("source_excerpt"):
            section_content = section_content_by_id.get(case["id"], "")
            if _normalize_for_match(case["source_excerpt"]) not in _normalize_for_match(section_content):
                dropped["excerpt"] += 1
                continue

        norm_q = _normalize_question(case["question"])
        if norm_q in seen_questions:
            dropped["dup"] += 1
            continue
        seen_questions.add(norm_q)
        survivors.append(case)

    print(
        f"\n[필터] 제외 — source_excerpt 불일치: {dropped['excerpt']}건, "
        f"질문 너무 짧음: {dropped['short']}건, 중복: {dropped['dup']}건"
    )
    return survivors


# ── 출력 ────────────────────────────────────────────────────────────────

def write_jsonl(path: Path, cases: List[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")


def print_counts(label: str, cases: List[Dict]) -> None:
    counts: Dict[str, int] = {}
    for c in cases:
        counts[c["category"]] = counts.get(c["category"], 0) + 1
    total = len(cases)
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"{label}: 총 {total}건 ({breakdown})")


def main():
    parser = argparse.ArgumentParser(description="골든 평가셋 초안 생성기 (#63 1단계)")
    parser.add_argument("--docs-root", default="/app/docs_repo")
    parser.add_argument("--limit-files", type=int, default=0)
    parser.add_argument("--target-manual-sections", type=int, default=45)
    parser.add_argument("--target-live", type=int, default=_TARGET_LIVE)
    parser.add_argument("--target-abstain", type=int, default=_TARGET_ABSTAIN)
    parser.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 절 목록만 출력")
    parser.add_argument(
        "--refilter", action="store_true",
        help="LLM 재호출 없이 기존 golden_draft.jsonl에 품질 필터만 다시 적용해 golden.jsonl을 갱신",
    )
    args = parser.parse_args()

    docs_root = Path(args.docs_root)
    files = find_manual_files(docs_root, args.limit_files or None)
    print(f"매뉴얼 파일 {len(files)}개 발견 (docs_root={docs_root})")
    if not files:
        print("[오류] 매뉴얼 파일을 찾을 수 없습니다 — --docs-root를 확인하세요.")
        sys.exit(1)

    by_file, abstain_by_file = build_candidates(files)
    selected_sections = select_sections(by_file, args.target_manual_sections)
    print(f"절 {sum(len(v) for v in by_file.values())}개 중 {len(selected_sections)}개 선택 (파일 {len(by_file)}개에 고르게 배분)")
    print(f"'확인이 필요한 항목' 절 보유 파일(abstain 우선 소스): {len(abstain_by_file)}개")

    if args.refilter:
        if not _DRAFT_PATH.is_file():
            print(f"[오류] {_DRAFT_PATH}가 없습니다 — 먼저 LLM 생성을 한 번 실행하세요.")
            sys.exit(1)
        draft_cases = [json.loads(line) for line in _DRAFT_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
        # notes 필드("relpath / heading")로 절 본문을 파일에서 다시 찾음 — LLM 재호출 없음.
        heading_content_by_relpath: Dict[str, Dict[str, str]] = {
            relpath: {c["heading"]: c["content"] for c in cands} for relpath, cands in by_file.items()
        }
        section_content_by_id = {}
        for case in draft_cases:
            if case.get("category") != "manual" or " / " not in (case.get("notes") or ""):
                continue
            relpath, heading = case["notes"].split(" / ", 1)
            content = heading_content_by_relpath.get(relpath, {}).get(heading, "")
            if content:
                section_content_by_id[case["id"]] = content
        golden_cases = apply_quality_filter(draft_cases, section_content_by_id)
        write_jsonl(_GOLDEN_PATH, golden_cases)
        print(f"golden 재생성: {_GOLDEN_PATH}")
        print_counts("초안(golden_draft.jsonl, 재사용)", draft_cases)
        print_counts("필터 통과(golden.jsonl, reviewed=false)", golden_cases)
        return

    if args.dry_run:
        for s in selected_sections:
            print(f"  {s['relpath']:50s} | {s['heading'][:40]:40s} | {len(s['content'])}자")
        for key in sorted(abstain_by_file):
            print(f"  [abstain 소스] {key} | {len(abstain_by_file[key]['content'])}자")
        db_path = settings.APP_DATA_DB_PATH
        try:
            con = sqlite3.connect(db_path)
            hub_n = con.execute("SELECT COUNT(*) FROM hub_replies WHERE is_active=1").fetchone()[0]
            live_n = con.execute(
                "SELECT COUNT(DISTINCT question) FROM chat_history WHERE is_no_answer=0 AND length(question)>8"
            ).fetchone()[0]
            con.close()
            print(f"\nhub_replies(is_active=1): {hub_n}건 / chat_history 후보: {live_n}건 (dry-run — LLM 미호출)")
        except sqlite3.Error as e:
            print(f"[경고] DB 조회 실패: {e}")
        return

    from company_llm_rag.llm.openai_provider import OpenAIProvider
    llm = OpenAIProvider(default_model=_DRAFT_MODEL, default_temperature=_DRAFT_TEMPERATURE)

    print("\n=== 매뉴얼 질문 생성 ===")
    try:
        manual_cases, section_content_by_id = build_manual_cases(llm, selected_sections)
    except Exception as e:
        print(f"[오류] LLM 호출 실패 — 중단: {e}")
        sys.exit(1)

    print("\n=== Hub 케이스 생성 ===")
    try:
        hub_cases = build_hub_cases(llm, settings.APP_DATA_DB_PATH)
    except Exception as e:
        print(f"[오류] LLM 호출 실패 — 중단: {e}")
        sys.exit(1)

    print("\n=== 실사용 질문(live) 로드 ===")
    live_cases = build_live_cases(settings.APP_DATA_DB_PATH, args.target_live)
    print(f"  {len(live_cases)}건 로드 (중복 제거 후)")

    print("\n=== 거부(abstain) 케이스 생성 ===")
    try:
        abstain_cases = build_abstain_cases(llm, abstain_by_file, by_file, args.target_abstain)
    except Exception as e:
        print(f"[오류] LLM 호출 실패 — 중단: {e}")
        sys.exit(1)

    draft_cases = manual_cases + hub_cases + live_cases + abstain_cases
    write_jsonl(_DRAFT_PATH, draft_cases)
    print(f"\n초안 저장: {_DRAFT_PATH}")
    print_counts("초안(golden_draft.jsonl)", draft_cases)

    golden_cases = apply_quality_filter(draft_cases, section_content_by_id)
    write_jsonl(_GOLDEN_PATH, golden_cases)
    print(f"golden 저장: {_GOLDEN_PATH}")
    print_counts("필터 통과(golden.jsonl, reviewed=false)", golden_cases)


if __name__ == "__main__":
    main()
