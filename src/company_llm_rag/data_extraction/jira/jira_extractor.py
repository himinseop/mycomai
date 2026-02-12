import json
import requests
import os
import base64
from dotenv import load_dotenv
import sys

load_dotenv()

# Jira Cloud URL (e.g., 'https://your-company.atlassian.net')
JIRA_BASE_URL = os.getenv('JIRA_BASE_URL')
JIRA_API_TOKEN = os.getenv('JIRA_API_TOKEN')
JIRA_EMAIL = os.getenv('JIRA_EMAIL')
JIRA_PROJECT_KEYS = os.getenv('JIRA_PROJECT_KEY', "").split(',')
# Number of days to look back for updates (None means all time)
LOOKBACK_DAYS = os.getenv('LOOKBACK_DAYS')

if not all([JIRA_BASE_URL, JIRA_API_TOKEN, JIRA_EMAIL]):
    print("Please set JIRA_BASE_URL, JIRA_API_TOKEN, and JIRA_EMAIL environment variables.", file=sys.stderr)
    exit(1)

HEADERS = {
    "Accept": "application/json",
    "Authorization": f"Basic {base64.b64encode(f'{JIRA_EMAIL}:{JIRA_API_TOKEN}'.encode()).decode()}"
}

def get_all_projects():
    """Fetches all projects available to the user."""
    url = f"{JIRA_BASE_URL}/rest/api/3/project"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.json()

def get_issues_for_project(project_key):
    """
    Fetches issues for a given project. Uses LOOKBACK_DAYS for incremental updates if set.
    """
    all_issues = []
    start_at = 0
    max_results = 50 

    jql = f"project = \"{project_key}\""
    if LOOKBACK_DAYS:
        jql += f" AND updated >= \"-{LOOKBACK_DAYS}d\""
    jql += " ORDER BY updated DESC"

    while True:
        url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
        params = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": max_results,
            "fields": "summary,description,comment,status,priority,reporter,assignee,issuetype,created,updated"
        }
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        data = response.json()
        issues = data.get('issues', [])
        
        if not issues:
            break
        
        all_issues.extend(issues)
        start_at += len(issues)

        if start_at >= data.get('total', 0):
            break
            
    return all_issues

def main():
    target_projects = [k.strip() for k in JIRA_PROJECT_KEYS if k.strip()]
    
    if not target_projects:
        print("No JIRA_PROJECT_KEY specified. Discovering all accessible projects...", file=sys.stderr)
        try:
            projects = get_all_projects()
            target_projects = [p['key'] for p in projects]
            print(f"Discovered {len(target_projects)} projects: {', '.join(target_projects)}", file=sys.stderr)
        except Exception as e:
            print(f"Error discovering projects: {e}", file=sys.stderr)
            return

    for i, project_key in enumerate(target_projects):
        print(f"[{i+1}/{len(target_projects)}] Processing Jira project: {project_key}...", file=sys.stderr)
        try:
            issues_data = get_issues_for_project(project_key)
            if issues_data:
                print(f"  - Found {len(issues_data)} issues.", file=sys.stderr)
                # Output in JSON Lines format
                for issue in issues_data:
                    try:
                        fields = issue.get('fields')
                        if not fields:
                            print(f"  - Skipping issue {issue.get('key')} due to missing fields.", file=sys.stderr)
                            continue

                        # Extract relevant fields and format as a flat structure for RAG
                        description = fields.get('description')
                        if description is None:
                            description = ""
                        
                        extracted_data_schema = {
                            "id": f"jira-{issue.get('id')}",
                            "source": "jira",
                            "source_id": issue.get('id'),
                            "url": f"{JIRA_BASE_URL}/browse/{issue.get('key')}",
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
                        print(f"  - Error processing issue {issue.get('key', 'unknown')}: {inner_e}", file=sys.stderr)
                        continue
            else:
                print(f"No issues found for project {project_key} or unexpected response format.", file=sys.stderr)
        except requests.exceptions.RequestException as e:
            print(f"Error fetching issues for project {project_key}: {e}", file=sys.stderr)
            if e.response:
                print(f"Response status: {e.response.status_code}", file=sys.stderr)
                print(f"Response body: {e.response.text}", file=sys.stderr)
        except Exception as e:
            print(f"An unexpected error occurred for project {project_key}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
