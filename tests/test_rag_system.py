"""
RAG 시스템 테스트
"""

import pytest
from company_llm_rag.rag_system import build_rag_prompt


class TestBuildRAGPrompt:
    """build_rag_prompt 함수 테스트"""

    def test_build_prompt_empty_docs(self):
        """빈 문서 리스트로 프롬프트 생성 테스트"""
        query = "What is the capital of France?"
        docs = []

        result = build_rag_prompt(query, docs)

        assert query in result
        assert "User Query" in result
        assert "Answer" in result

    def test_build_prompt_single_doc(self):
        """단일 문서로 프롬프트 생성 테스트"""
        query = "Test query"
        docs = [
            {
                "content": "This is test content",
                "metadata": {
                    "source": "test",
                    "title": "Test Document",
                    "url": "http://test.com"
                }
            }
        ]

        result = build_rag_prompt(query, docs)

        assert query in result
        assert "This is test content" in result
        assert "Test Document" in result
        assert "http://test.com" in result
        assert "Document 1" in result

    def test_build_prompt_multiple_docs(self):
        """여러 문서로 프롬프트 생성 테스트"""
        query = "Test query"
        docs = [
            {
                "content": "Content 1",
                "metadata": {
                    "source": "source1",
                    "title": "Doc 1",
                    "url": "url1"
                }
            },
            {
                "content": "Content 2",
                "metadata": {
                    "source": "source2",
                    "title": "Doc 2",
                    "url": "url2"
                }
            }
        ]

        result = build_rag_prompt(query, docs)

        assert "Content 1" in result
        assert "Content 2" in result
        assert "Document 1" in result
        assert "Document 2" in result

    def test_build_prompt_with_jira_comments(self):
        """Jira 댓글이 포함된 문서 테스트"""
        query = "Test query"
        docs = [
            {
                "content": "Issue description",
                "metadata": {
                    "source": "jira",
                    "title": "PROJ-123",
                    "url": "url",
                    "comments": [
                        {
                            "author": "John",
                            "created_at": "2026-01-01",
                            "content": "This is a comment"
                        }
                    ]
                }
            }
        ]

        result = build_rag_prompt(query, docs)

        assert "Issue description" in result
        assert "Comment by John" in result
        assert "This is a comment" in result

    def test_build_prompt_with_teams_replies(self):
        """Teams 답글이 포함된 문서 테스트"""
        query = "Test query"
        docs = [
            {
                "content": "Original message",
                "metadata": {
                    "source": "teams",
                    "title": "Teams Message",
                    "url": "url",
                    "replies": [
                        {
                            "sender": "Alice",
                            "created_at": "2026-01-01",
                            "content": "Reply message"
                        }
                    ]
                }
            }
        ]

        result = build_rag_prompt(query, docs)

        assert "Original message" in result
        assert "Reply by Alice" in result
        assert "Reply message" in result
