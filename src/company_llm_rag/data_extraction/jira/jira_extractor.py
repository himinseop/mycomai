import json
import requests
import sys
import time
from datetime import timedelta

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

_PROGRESS_EVERY = 100

def _fmt_elapsed(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


def _adf_to_text(node) -> str:
    """ADF(Atlassian Document Format) 노드를 평문 텍스트로 변환합니다."""
    if isinstance(node, dict):
        if node.get('type') == 'text':
            return node.get('text', '')
        return '\n'.join(filter(None, (_adf_to_text(c) for c in node.get('content', []))))
    if isinstance(node, list):
        return '\n'.join(filter(None, (_adf_to_text(item) for item in node)))
    return ''


def get_all_projects():
    """사용자가 접근 가능한 모든 프로젝트를 가져옵니다."""
    url = f"{settings.JIRA_BASE_URL}/rest/api/3/project"
    headers = settings.get_auth_header("jira")
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()

def get_issues_for_project(project_key):
    """
    주어진 프로젝트의 이슈를 가져옵니다.
    LOOKBACK_DAYS가 설정된 경우 증분 업데이트를 수행합니다.

    Args:
        project_key: Jira 프로젝트 키

    Returns:
        이슈 리스트
    """
    all_issues = []
    max_results = settings.JIRA_MAX_RESULTS

    jql = f"project = \"{project_key}\" ORDER BY updated DESC"

    headers = settings.get_auth_header("jira")
    next_page_token = None

    while True:
        # Use API v3 with nextPageToken-based pagination
        url = f"{settings.JIRA_BASE_URL}/rest/api/3/search/jql"
        params = {
            "jql": jql,
            "maxResults": max_results,
            "fields": (
                "summary,description,comment,status,priority,"
                "reporter,assignee,issuetype,created,updated,"
                "duedate,labels,issuelinks,attachment,"
                "customfield_10015"  # start date (Jira Cloud 기본 커스텀 필드)
            )
        }

        # Add nextPageToken if we have one (for subsequent pages)
        if next_page_token:
            params["nextPageToken"] = next_page_token

        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        issues = data.get('issues', [])
        is_last = data.get('isLast', True)
        next_page_token = data.get('nextPageToken')

        if not issues:
            logger.debug(f"No more issues found. Fetched {len(all_issues)} total.")
            break

        all_issues.extend(issues)
        logger.debug(f"Fetched {len(issues)} issues, total so far: {len(all_issues)}, isLast: {is_last}")

        # Check if this is the last page
        if is_last:
            logger.debug(f"Reached last page. Total issues: {len(all_issues)}")
            break

    return all_issues

def main():
    target_projects = settings.JIRA_PROJECT_KEYS

    if not target_projects:
        logger.info("No JIRA_PROJECT_KEY specified. Discovering all accessible projects...")
        try:
            projects = get_all_projects()
            target_projects = [p['key'] for p in projects]
            logger.info(f"Discovered {len(target_projects)} projects: {', '.join(target_projects)}")
        except Exception as e:
            logger.error(f"Error discovering projects: {e}", exc_info=True)
            return

    for i, project_key in enumerate(target_projects):
        logger.info(f"[{i+1}/{len(target_projects)}] Processing Jira project: {project_key}...")
        try:
            issues_data = get_issues_for_project(project_key)
            if issues_data:
                total = len(issues_data)
                logger.info(f"[Jira][{project_key}] {total}개 이슈 발견. 수집 시작...")
                start_time = time.time()
                for j, issue in enumerate(issues_data, 1):
                    try:
                        fields = issue.get('fields')
                        if not fields:
                            logger.warning(f"  - Skipping issue {issue.get('key')} due to missing fields.")
                            continue

                        # description: ADF → 평문 변환
                        description = fields.get('description') or ''
                        if isinstance(description, dict):
                            description = _adf_to_text(description)

                        # comments: ADF → 평문 변환 후 content에 합치기
                        comment_obj = fields.get('comment', {})
                        comments = comment_obj.get('comments', []) if comment_obj else []
                        comment_blocks = []
                        for comment in comments:
                            if not comment:
                                continue
                            body = comment.get('body') or ''
                            if isinstance(body, dict):
                                body = _adf_to_text(body)
                            if not body.strip():
                                continue
                            author = comment.get('author', {}).get('displayName', 'Unknown') if comment.get('author') else 'Unknown'
                            date = (comment.get('created') or '')[:10]
                            comment_blocks.append(f"[Comment by {author} on {date}]\n{body.strip()}")

                        # 날짜 필드
                        due_date    = fields.get('duedate') or ''
                        start_date  = fields.get('customfield_10015') or ''

                        # 담당자 / 보고자
                        assignee = fields.get('assignee', {}).get('displayName') if fields.get('assignee') else ''
                        reporter = fields.get('reporter', {}).get('displayName') if fields.get('reporter') else ''

                        # 레이블
                        labels = fields.get('labels') or []

                        # 연결된 이슈
                        link_lines = []
                        for link in (fields.get('issuelinks') or []):
                            link_type = link.get('type', {}).get('name', '')
                            if 'outwardIssue' in link:
                                li = link['outwardIssue']
                                relation = link.get('type', {}).get('outward', link_type)
                            elif 'inwardIssue' in link:
                                li = link['inwardIssue']
                                relation = link.get('type', {}).get('inward', link_type)
                            else:
                                continue
                            li_key     = li.get('key', '')
                            li_summary = li.get('fields', {}).get('summary', '')
                            li_status  = li.get('fields', {}).get('status', {}).get('name', '')
                            link_lines.append(f"{li_key} ({relation}): {li_summary} [{li_status}]")

                        # 첨부파일 목록
                        attachments = [
                            a.get('filename', '') for a in (fields.get('attachment') or []) if a.get('filename')
                        ]

                        # content 조합
                        extra_parts = []
                        if assignee:   extra_parts.append(f"담당자: {assignee}")
                        if reporter:   extra_parts.append(f"보고자: {reporter}")
                        if labels:     extra_parts.append(f"레이블: {', '.join(labels)}")
                        if start_date: extra_parts.append(f"시작일: {start_date[:10]}")
                        if due_date:   extra_parts.append(f"기한: {due_date[:10]}")
                        if link_lines:
                            extra_parts.append("연결된 이슈:\n" + '\n'.join(f"  - {l}" for l in link_lines))
                        if attachments:
                            extra_parts.append("첨부파일:\n" + '\n'.join(f"  - {a}" for a in attachments))

                        content_parts = [p for p in [description, '\n'.join(extra_parts)] if p]
                        if comment_blocks:
                            content_parts.append('\n\n'.join(comment_blocks))
                        content = '\n\n'.join(content_parts)

                        extracted_data_schema = {
                            "id": f"jira-{issue.get('id')}",
                            "source": "jira",
                            "source_id": issue.get('id'),
                            "url": f"{settings.JIRA_BASE_URL}/browse/{issue.get('key')}",
                            "title": fields.get('summary', 'No Summary'),
                            "content": content,
                            "content_type": "issue",
                            "created_at": fields.get('created'),
                            "updated_at": fields.get('updated'),
                            "author": reporter or "Unknown",
                            "metadata": {
                                "jira_project_key": project_key,
                                "jira_issue_key": issue.get('key'),
                                "jira_issue_type": fields.get('issuetype', {}).get('name') if fields.get('issuetype') else "Unknown",
                                "status": fields.get('status', {}).get('name') if fields.get('status') else "Unknown",
                                "priority": fields.get('priority', {}).get('name') if fields.get('priority') else "None",
                                "assignee": assignee or "Unassigned",
                                "reporter": reporter or "Unknown",
                                "labels": ', '.join(labels),
                                "start_date": start_date[:10] if start_date else '',
                                "due_date": due_date[:10] if due_date else '',
                                "linked_issues": ', '.join(l.split(':')[0] for l in link_lines),
                                "attachment_count": len(attachments),
                                "attachments": ', '.join(attachments),
                                "comment_count": len(comment_blocks),
                            }
                        }
                        
                        print(json.dumps(extracted_data_schema, ensure_ascii=False))
                        if j % _PROGRESS_EVERY == 0 or j == total:
                            pct = int(j / total * 100)
                            elapsed = _fmt_elapsed(time.time() - start_time)
                            logger.info(f"[Jira][{project_key}] {j}/{total} ({pct}%) | 경과: {elapsed}")
                    except Exception as inner_e:
                        logger.error(f"  - Error processing issue {issue.get('key', 'unknown')}: {inner_e}", exc_info=True)
                        continue
                logger.info(f"[Jira][{project_key}] 완료: {total}개 | 소요: {_fmt_elapsed(time.time() - start_time)}")
            else:
                logger.warning(f"No issues found for project {project_key} or unexpected response format.")
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching issues for project {project_key}: {e}")
            if e.response:
                logger.error(f"Response status: {e.response.status_code}")
                logger.debug(f"Response body: {e.response.text}")
        except Exception as e:
            logger.error(f"An unexpected error occurred for project {project_key}: {e}", exc_info=True)

if __name__ == "__main__":
    main()
