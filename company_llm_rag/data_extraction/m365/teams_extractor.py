import os
import json
import requests
import msal # Assuming 'msal' is installed (pip install msal)
from dotenv import load_dotenv # Added for loading .env file
import sys # Added for sys.stderr

load_dotenv() # Load environment variables from .env file

# Environment variables for Azure AD App registration
TENANT_ID = os.getenv('TENANT_ID')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')

# Teams specific environment variable
TEAMS_GROUP_NAME = os.getenv('TEAMS_GROUP_NAME') # e.g., 'My Team'

# Authority and Scope for Microsoft Graph API
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPE = ["https://graph.microsoft.com/.default"] # Requesting default permissions

if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET, TEAMS_GROUP_NAME]):
    print("Please set TENANT_ID, CLIENT_ID, CLIENT_SECRET, and TEAMS_GROUP_NAME environment variables.", file=sys.stderr)
    exit(1)

# Initialize MSAL ConfidentialClientApplication
app = msal.ConfidentialClientApplication(
    CLIENT_ID,
    authority=AUTHORITY,
    client_credential=CLIENT_SECRET
)

def get_access_token():
    """Acquires an access token for Microsoft Graph API."""
    result = app.acquire_token_for_client(scopes=SCOPE)
    if "access_token" in result:
        return result["access_token"]
    else:
        raise Exception(f"Could not acquire access token: {result.get('error_description')}")

def call_graph_api(endpoint, access_token):
    """Makes a GET request to the Microsoft Graph API."""
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    response = requests.get(endpoint, headers=headers)
    response.raise_for_status()
    return response.json()

def get_team_id_by_display_name(team_display_name, access_token):
    """Gets the Team (Microsoft 365 Group) ID by its display name."""
    endpoint = f"https://graph.microsoft.com/v1.0/groups?$filter=displayName eq '{team_display_name}'&$select=id,displayName"
    response_data = call_graph_api(endpoint, access_token)
    groups = response_data.get('value', [])
    if groups:
        return groups[0]['id']
    raise Exception(f"Team with display name '{team_display_name}' not found.")

def get_channels_for_team(team_id, access_token):
    """Gets all channels for a given Team."""
    all_channels = []
    endpoint = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels"
    
    while endpoint:
        response_data = call_graph_api(endpoint, access_token)
        channels = response_data.get('value', [])
        all_channels.extend(channels)
        endpoint = response_data.get('@odata.nextLink')
    return all_channels

def get_channel_messages(team_id, channel_id, access_token):
    """Gets all messages (and replies) for a given channel."""
    all_messages = []
    # Using /messages endpoint to get top-level messages
    endpoint = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}/messages?$expand=replies"
    
    while endpoint:
        response_data = call_graph_api(endpoint, access_token)
        messages = response_data.get('value', [])
        all_messages.extend(messages)
        endpoint = response_data.get('@odata.nextLink')
    return all_messages

def main():
    try:
        access_token = get_access_token()
        print("Successfully acquired access token.", file=sys.stderr)

        team_id = get_team_id_by_display_name(TEAMS_GROUP_NAME, access_token)
        print(f"Team ID for '{TEAMS_GROUP_NAME}': {team_id}", file=sys.stderr)

        channels = get_channels_for_team(team_id, access_token)
        print(f"Found {len(channels)} channels in Team '{TEAMS_GROUP_NAME}'.", file=sys.stderr)

        for channel in channels:
            channel_id = channel['id']
            channel_display_name = channel['displayName']
            print(f"Fetching messages from channel: {channel_display_name}", file=sys.stderr)

            messages = get_channel_messages(team_id, channel_id, access_token)
            for message in messages:
                author_info = message.get('from', {})
                author_name = "Unknown"
                if author_info:
                    if author_info.get('user'):
                        author_name = author_info['user'].get('displayName', "Unknown User")
                    elif author_info.get('application'):
                        author_name = author_info['application'].get('displayName', "Unknown Application")
                
                extracted_data_schema = {
                    "id": f"teams-{message.get('id')}",
                    "source": "teams",
                    "source_id": message.get('id'),
                    "url": None, # Teams messages don't have a direct public URL like Jira/Confluence/SharePoint files
                    "title": message.get('subject') or f"Teams Message in {channel_display_name}",
                    "content": message.get('body', {}).get('content'),
                    "content_type": "message",
                    "created_at": message.get('createdDateTime'),
                    "updated_at": message.get('lastModifiedDateTime'),
                    "author": author_name,
                    "metadata": {
                        "teams_team_name": TEAMS_GROUP_NAME,
                        "teams_team_id": team_id,
                        "teams_channel_name": channel_display_name,
                        "teams_channel_id": channel_id,
                        "message_type": message.get('messageType'),
                        "replies": []
                    }
                }

                # Process replies
                replies = message.get('replies', [])
                for reply in replies:
                    reply_author_info = reply.get('from', {})
                    reply_author_name = "Unknown"
                    if reply_author_info:
                        if reply_author_info.get('user'):
                            reply_author_name = reply_author_info['user'].get('displayName', "Unknown User")
                        elif reply_author_info.get('application'):
                            reply_author_name = reply_author_info['application'].get('displayName', "Unknown Application")
                    
                    extracted_data_schema["metadata"]["replies"].append({
                        "id": reply.get('id'),
                        "author": reply_author_name,
                        "created_at": reply.get('createdDateTime'),
                        "content": reply.get('body', {}).get('content')
                    })
                
                print(json.dumps(extracted_data_schema, ensure_ascii=False))
        
    except requests.exceptions.RequestException as e:
        print(f"Error calling Microsoft Graph API: {e}", file=sys.stderr)
        if e.response:
            print(f"Response status: {e.response.status_code}", file=sys.stderr)
            print(f"Response body: {e.response.text}", file=sys.stderr)
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()