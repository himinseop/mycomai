"""
공통 타입 정의 (TypedDict)

rag_system, retrieval_module, history_store, web_app 등에서 공유합니다.
"""

from typing import List, Optional
from typing_extensions import TypedDict


class DocumentMetadata(TypedDict, total=False):
    """ChromaDB에 저장되는 문서 메타데이터 스키마."""
    source: str
    title: str
    url: str
    author: str
    created_at: str
    updated_at: str
    content_type: str
    content_hash: str

    # Jira
    jira_issue_key: str
    jira_project_key: str
    status: str
    assignee: str
    jira_issue_type: str

    # Confluence
    confluence_space_key: str
    confluence_space_name: str
    confluence_ancestors: str

    # Teams
    teams_team_id: str
    teams_team_name: str
    teams_channel_id: str
    teams_channel_name: str
    teams_chat_id: str
    teams_chat_topic: str

    # SharePoint / local
    file_extension: str
    source_id: str
    original_doc_id: str


class RetrievedDocument(TypedDict):
    """retrieval_module이 반환하는 검색 결과 문서."""
    content: str
    metadata: DocumentMetadata
    _distance: float


class Reference(TypedDict, total=False):
    """RAG 답변에 첨부되는 참고 문서 링크 정보."""
    title: str
    url: str
    source: str
    content_type: str
    issue_key: str       # Jira: "WMPO-1234"
    space_name: str      # Confluence: 스페이스 표시 이름
    ancestors: str       # Confluence: "폴더1 / 폴더2"
    team_name: str       # Teams: 팀 이름
    channel_name: str    # Teams: 채널 이름
    chat_topic: str      # Teams: 일반 채팅방 이름
    author: str
    created_at: str
    snippet: str         # Teams: 대화 내용 일부
