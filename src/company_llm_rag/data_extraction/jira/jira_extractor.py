import json
import requests
import sys

from company_llm_rag.config import settings
from company_llm_rag.logger import get_logger

logger = get_logger(__name__)

def get_all_projects():
    """사용자가 접근 가능한 모든 프로젝트를 가져옵니다."""
    url = f"{settings.JIRA_BASE_URL}/rest/api/3/project"
    headers = settings.get_auth_header("jira")
    response = requests.get(url, headers=headers)
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

    jql = f"project = \"{project_key}\""
    if settings.LOOKBACK_DAYS:
        jql += f" AND updated >= \"-{settings.LOOKBACK_DAYS}d\""
    jql += " ORDER BY updated DESC"

    headers = settings.get_auth_header("jira")
    next_page_token = None

    while True:
        # Use API v3 with nextPageToken-based pagination
        url = f"{settings.JIRA_BASE_URL}/rest/api/3/search/jql"
        params = {
            "jql": jql,
            "maxResults": max_results,
            "fields": "summary,description,comment,status,priority,reporter,assignee,issuetype,created,updated"
        }

        # Add nextPageToken if we have one (for subsequent pages)
        if next_page_token:
            params["nextPageToken"] = next_page_token

        response = requests.get(url, headers=headers, params=params)
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
                logger.info(f"  - Found {len(issues_data)} issues.")
                # Output in JSON Lines format
                for issue in issues_data:
                    try:
                        fields = issue.get('fields')
                        if not fields:
                            logger.warning(f"  - Skipping issue {issue.get('key')} due to missing fields.")
                            continue

                        # Extract relevant fields and format as a flat structure for RAG
                        description = fields.get('description')
                        if description is None:
                            description = ""
                        
                        extracted_data_schema = {
                            "id": f"jira-{issue.get('id')}",
                            "source": "jira",
                            "source_id": issue.get('id'),
                            "url": f"{settings.JIRA_BASE_URL}/browse/{issue.get('key')}",
                            "title": fields.get('summary', 'No Summary'),
                            "content": description,
                            "content_type": "issue",
                            "created_at": fields.get('created'),
                            "updated_at": fields.get('updated'),
                            "author": fields.get('reporter', {}).get('displayName') if fields.get('reporter') else "Unknown",
                            "metadata": {
                                "jira_project_key": project_key,
                                "jira_issue_key": issue.get('key'),
                                "jira_issue_type": fields.get('issuetype', {}).get('name') if fields.get('issuetype') else "Unknown",
                                "status": fields.get('status', {}).get('name') if fields.get('status') else "Unknown",
                                "priority": fields.get('priority', {}).get('name') if fields.get('priority') else "None",
                                "assignee": fields.get('assignee', {}).get('displayName') if fields.get('assignee') else "Unassigned",
                                "comments": []
                            }
                        }
                        # Process comments
                        comment_obj = fields.get('comment', {})
                        comments = comment_obj.get('comments', []) if comment_obj else []
                        for comment in comments:
                            if not comment: continue
                            extracted_data_schema["metadata"]["comments"].append({
                                "id": comment.get('id'),
                                "author": comment.get('author', {}).get('displayName') if comment.get('author') else "Unknown",
                                "created_at": comment.get('created'),
                                "content": comment.get('body')
                            })
                        
                        print(json.dumps(extracted_data_schema, ensure_ascii=False))
                    except Exception as inner_e:
                        logger.error(f"  - Error processing issue {issue.get('key', 'unknown')}: {inner_e}", exc_info=True)
                        continue
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
