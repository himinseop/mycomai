import os
import json
import requests
import msal # Assuming 'msal' is installed (pip install msal)
from urllib.parse import urlparse
from dotenv import load_dotenv # Added for loading .env file
import sys # Added for sys.stderr

load_dotenv() # Load environment variables from .env file

# Environment variables for Azure AD App registration
TENANT_ID = os.getenv('TENANT_ID')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')

# SharePoint specific environment variable
SHAREPOINT_SITE_NAME = os.getenv('SHAREPOINT_SITE_NAME') # e.g., 'o2olab'

# Authority and Scope for Microsoft Graph API
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPE = ["https://graph.microsoft.com/.default"] # Requesting default permissions

if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET, SHAREPOINT_SITE_NAME]):
    print("Please set TENANT_ID, CLIENT_ID, CLIENT_SECRET, and SHAREPOINT_SITE_NAME environment variables.")
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
    if "access_token" in result and result["access_token"]: # Check if key exists AND value is not empty
        return result["access_token"]
    else:
        print(f"MSAL acquire_token_for_client result: {result}", file=sys.stderr)
        error_msg = result.get('error_description') or result.get('error') or "Access token is empty or could not be acquired."
        raise Exception(f"Could not acquire access token: {error_msg}")

def call_graph_api(endpoint, access_token):
    """Makes a GET request to the Microsoft Graph API."""
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    response = requests.get(endpoint, headers=headers)
    response.raise_for_status()
    return response.json()

def get_sharepoint_site_id(site_name, access_token):
    """Gets the SharePoint site ID by its name."""
    # First, try to find the site using the search endpoint which is often more reliable
    # with application permissions for non-root sites.
    try:
        search_endpoint = f"https://graph.microsoft.com/v1.0/sites?search='{site_name}'"
        search_results = call_graph_api(search_endpoint, access_token)
        sites = search_results.get('value', [])
        if sites:
            # Look for an exact match on display name
            for site in sites:
                if site.get('displayName').lower() == site_name.lower():
                    return site['id']
            # If no exact display name match, take the first one if any
            return sites[0]['id']
    except requests.exceptions.HTTPError as e:
        # If search fails, it might be due to permissions or the search mechanism itself
        print(f"SharePoint site search failed: {e}. Falling back to hostname-based lookup.", file=sys.stderr)
        # Continue to original hostname-based lookup

    # Fallback to original hostname-based lookup if search didn't work or failed
    root_site_info = call_graph_api("https://graph.microsoft.com/v1.0/sites/root", access_token)
    hostname = urlparse(root_site_info['webUrl']).hostname

    endpoint = f"https://graph.microsoft.com/v1.0/sites/{hostname}:/sites/{site_name}"
    try:
        site_info = call_graph_api(endpoint, access_token)
        return site_info['id']
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            endpoint = f"https://graph.microsoft.com/v1.0/sites/{hostname}:/{site_name}"
            site_info = call_graph_api(endpoint, access_token)
            return site_info['id']
        else:
            raise

def get_drive_id_for_site(site_id, access_token):
    """Gets the default document drive ID for a SharePoint site."""
    endpoint = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive"
    drive_info = call_graph_api(endpoint, access_token)
    return drive_info['id']

def get_files_in_folder(drive_id, folder_path, access_token):
    """Recursively gets all files within a SharePoint folder."""
    all_files_metadata = []
    
    # Start with the root folder if folder_path is empty or refers to root
    if folder_path == "" or folder_path == "/":
        endpoint = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/children"
    else:
        # Encode the folder path for URL
        encoded_folder_path = requests.utils.quote(folder_path.lstrip('/'))
        endpoint = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{encoded_folder_path}:/children"

    while endpoint:
        response_data = call_graph_api(endpoint, access_token)
        items = response_data.get('value', [])
        
        for item in items:
            if 'file' in item: # It's a file
                all_files_metadata.append(item)
            elif 'folder' in item: # It's a folder, recurse
                # Construct new folder_path for recursion
                new_folder_path = os.path.join(folder_path, item['name'])
                all_files_metadata.extend(get_files_in_folder(drive_id, new_folder_path, access_token))
        
        endpoint = response_data.get('@odata.nextLink') # For pagination

    return all_files_metadata

def download_file_content(download_url, access_token):
    """Downloads the content of a file from a given download URL."""
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    response = requests.get(download_url, headers=headers)
    response.raise_for_status()
    return response.text # Assuming text-based content

def main():
    try:
        access_token = get_access_token()
        print("Successfully acquired access token.", file=sys.stderr)

        site_id = get_sharepoint_site_id(SHAREPOINT_SITE_NAME, access_token)
        print(f"SharePoint Site ID for '{SHAREPOINT_SITE_NAME}': {site_id}", file=sys.stderr)

        drive_id = get_drive_id_for_site(site_id, access_token)
        print(f"Default Drive ID for site: {drive_id}", file=sys.stderr)

        print(f"Fetching files from SharePoint site '{SHAREPOINT_SITE_NAME}'...", file=sys.stderr)
        files_metadata = get_files_in_folder(drive_id, "", access_token) # Start from root of the drive

        if files_metadata:
            for file_meta in files_metadata:
                file_name = file_meta.get('name')
                file_id = file_meta.get('id')
                file_web_url = file_meta.get('webUrl')
                file_download_url = file_meta.get('@microsoft.graph.downloadUrl')
                file_path = file_meta.get('parentReference', {}).get('path')
                
                content_to_store = None
                mime_type = file_meta.get('file', {}).get('mimeType')
                file_size = file_meta.get('size')

                # Attempt to download content only for supported text-based files
                # This is a simplified check; a robust solution would use a proper file type parser
                if file_download_url and mime_type in [
                    "text/plain", "text/markdown", "application/json", "application/xml",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # .docx
                    "application/pdf" # For PDF, you'd need a separate library like PyPDF2
                ]:
                    try:
                        # For docx/pdf, content won't be directly text from downloadUrl without conversion.
                        # This part would need external libraries to parse non-plain text files.
                        # For now, we'll just download as text and it will likely fail for binary formats.
                        file_content = download_file_content(file_download_url, access_token)
                        content_to_store = file_content
                    except Exception as e:
                        content_to_store = f"[Error downloading or parsing content: {e}]"
                        print(f"Warning: Could not download/parse content for {file_name}: {e}", file=sys.stderr)
                elif file_download_url:
                    content_to_store = f"[Content not extracted: Unsupported MIME type {mime_type}]"
                else:
                    content_to_store = "[Content not available for download]"


                extracted_data_schema = {
                    "id": f"sharepoint-{file_id}",
                    "source": "sharepoint",
                    "source_id": file_id,
                    "url": file_web_url,
                    "title": file_name,
                    "content": content_to_store,
                    "content_type": "file",
                    "created_at": file_meta.get('createdDateTime'),
                    "updated_at": file_meta.get('lastModifiedDateTime'),
                    "author": file_meta.get('lastModifiedBy', {}).get('user', {}).get('displayName'), # Using lastModifiedBy for author
                    "metadata": {
                        "sharepoint_site_name": SHAREPOINT_SITE_NAME,
                        "sharepoint_file_path": file_path,
                        "mime_type": mime_type,
                        "size": file_size
                    }
                }
                
                print(json.dumps(extracted_data_schema, ensure_ascii=False))
        else:
            print(f"No files found in SharePoint site '{SHAREPOINT_SITE_NAME}'.", file=sys.stderr)

    except requests.exceptions.RequestException as e:
        print(f"Error calling Microsoft Graph API: {e}", file=sys.stderr)
        if e.response:
            print(f"Response status: {e.response.status_code}", file=sys.stderr)
            print(f"Response body: {e.response.text}", file=sys.stderr)
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
