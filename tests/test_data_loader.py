"""
Data Loader 모듈 테스트
"""

import pytest
from company_llm_rag.data_loader import chunk_content, _extract_text_from_adf_node, convert_adf_to_plain_text


class TestChunkContent:
    """chunk_content 함수 테스트"""

    def test_chunk_empty_content(self):
        """빈 콘텐츠 처리 테스트"""
        result = chunk_content("")
        assert result == []

    def test_chunk_small_content(self):
        """청크 크기보다 작은 콘텐츠 테스트"""
        content = "This is a small text"
        result = chunk_content(content, chunk_size=100)

        assert len(result) == 1
        assert result[0] == content

    def test_chunk_large_content(self):
        """청크 크기보다 큰 콘텐츠 테스트"""
        # 150 단어 생성
        words = [f"word{i}" for i in range(150)]
        content = " ".join(words)

        result = chunk_content(content, chunk_size=100, chunk_overlap=50)

        # 청크가 여러 개 생성되어야 함
        assert len(result) > 1
        # 각 청크가 문자열이어야 함
        assert all(isinstance(chunk, str) for chunk in result)

    def test_chunk_with_custom_size(self):
        """커스텀 청크 크기 테스트"""
        words = [f"word{i}" for i in range(50)]
        content = " ".join(words)

        result = chunk_content(content, chunk_size=20, chunk_overlap=5)

        # 청크가 생성되어야 함
        assert len(result) >= 1
        # 각 청크의 단어 수가 20을 초과하지 않아야 함
        for chunk in result:
            assert len(chunk.split()) <= 20

    def test_chunk_overlap(self):
        """청크 중복 테스트"""
        words = [f"word{i}" for i in range(120)]
        content = " ".join(words)

        result = chunk_content(content, chunk_size=100, chunk_overlap=50)

        # 청크가 2개 이상이어야 함
        assert len(result) >= 2


class TestADFConversion:
    """ADF (Atlassian Document Format) 변환 테스트"""

    def test_extract_simple_text(self):
        """간단한 텍스트 노드 추출 테스트"""
        node = {
            "type": "text",
            "text": "Hello World"
        }

        result = _extract_text_from_adf_node(node)
        assert "Hello World" in result

    def test_convert_adf_document(self):
        """ADF 문서 변환 테스트"""
        adf_doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "This is a paragraph"
                        }
                    ]
                }
            ]
        }

        result = convert_adf_to_plain_text(adf_doc)
        assert "This is a paragraph" in result

    def test_convert_invalid_adf(self):
        """잘못된 ADF 형식 처리 테스트"""
        invalid_adf = {"type": "invalid"}

        result = convert_adf_to_plain_text(invalid_adf)
        # Fallback으로 문자열 변환되어야 함
        assert isinstance(result, str)
