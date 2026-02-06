import json
import requests
import os
import base64 # Added for base64 encoding
from dotenv import load_dotenv
import sys # Added for sys.stderr

load_dotenv() # Load environment variables from .env file

# Jira Cloud URL (e.g., 'https://your-company.atlassian.net')
JIRA_BASE_URL = os.getenv('JIRA_BASE_URL')
JIRA_API_TOKEN = os.getenv('JIRA_API_TOKEN')
JIRA_EMAIL = os.getenv('JIRA_EMAIL')
JIRA_PROJECT_KEY = os.getenv('JIRA_PROJECT_KEY')

if not all([JIRA_BASE_URL, JIRA_API_TOKEN, JIRA_EMAIL, JIRA_PROJECT_KEY]):
    print("Please set JIRA_BASE_URL, JIRA_API_TOKEN, JIRA_EMAIL, and JIRA_PROJECT_KEY environment variables.", file=sys.stderr)
    exit(1)

HEADERS = {
    "Accept": "application/json",
    "Authorization": f"Basic {base64.b64encode(f'{JIRA_EMAIL}:{JIRA_API_TOKEN}'.encode()).decode()}"
}

def get_issues_for_project(project_key):
    """
    Fetches all issues for a given project from Jira Cloud.
    """
    all_issues = []
    start_at = 0
    max_results = 50 # Jira API default and maximum is 50 for search

    while True:
        url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
        params = {
            "jql": f"project = \"{project_key}\" ORDER BY created DESC",
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
    print(f"Fetching Jira issues for project: {JIRA_PROJECT_KEY}...", file=sys.stderr)
    try:
        issues_data = get_issues_for_project(JIRA_PROJECT_KEY)
        if issues_data:
            # Output in JSON Lines format
            for issue in issues_data:
                # Extract relevant fields and format as a flat structure for RAG
                extracted_data_schema = {
                    "id": f"jira-{issue.get('id')}",
                    "source": "jira",
                    "source_id": issue.get('id'),
                    "url": f"{JIRA_BASE_URL}/browse/{issue.get('key')}",
                    "title": issue['fields'].get('summary'),
                    "content": issue['fields'].get('description'),
                    "content_type": "issue",
                    "created_at": issue['fields'].get('created'),
                    "updated_at": issue['fields'].get('updated'),
                    "author": issue['fields'].get('reporter', {}).get('displayName'),
                    "metadata": {
                        "jira_project_key": JIRA_PROJECT_KEY,
                        "jira_issue_key": issue.get('key'),
                        "jira_issue_type": issue['fields'].get('issuetype', {}).get('name'),
                        "status": issue['fields'].get('status', {}).get('name'),
                        "priority": issue['fields'].get('priority', {}).get('name'),
                        "assignee": issue['fields'].get('assignee', {}).get('displayName'),
                        "comments": []
                    }
                }
                # Process comments
                comments = issue['fields'].get('comment', {}).get('comments', [])
                for comment in comments:
                    extracted_data_schema["metadata"]["comments"].append({
                        "id": comment.get('id'),
                        "author": comment.get('author', {}).get('displayName'),
                        "created_at": comment.get('created'),
                        "content": comment.get('body')
                    })
                
                print(json.dumps(extracted_data_schema, ensure_ascii=False))
        else:
            print(f"No issues found for project {JIRA_PROJECT_KEY} or unexpected response format.", file=sys.stderr)
    except requests.exceptions.RequestException as e:
        print(f"Error fetching issues: {e}", file=sys.stderr)
        if e.response:
            print(f"Response status: {e.response.status_code}", file=sys.stderr)
            print(f"Response body: {e.response.text}", file=sys.stderr)
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
