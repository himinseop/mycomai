import io
from typing import Optional

from company_llm_rag.logger import get_logger

logger = get_logger(__name__)


def extract_pdf_text(file_bytes: bytes) -> str:
    """
    PDF 바이너리에서 텍스트를 추출합니다.
    스캔본(이미지 PDF)처럼 텍스트 레이어가 없으면 빈 문자열을 반환합니다.

    Args:
        file_bytes: PDF 파일의 바이너리 데이터

    Returns:
        추출된 텍스트 (페이지별 구분)
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf가 설치되어 있지 않습니다. PDF 파싱을 건너뜁니다.")
        return ""

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = []
        for i, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(f"[Page {i}]\n{text.strip()}")
        return "\n\n".join(pages)
    except Exception as e:
        logger.warning(f"PDF 텍스트 추출 실패: {e}")
        return ""


def extract_pptx_text(file_bytes: bytes) -> str:
    """
    PPTX 바이너리에서 텍스트를 추출합니다.
    텍스트가 없는 슬라이드(이미지만 있는 슬라이드 등)는 건너뜁니다.

    Args:
        file_bytes: PPTX 파일의 바이너리 데이터

    Returns:
        추출된 텍스트 (슬라이드별 구분)
    """
    try:
        from pptx import Presentation
    except ImportError:
        logger.warning("python-pptx가 설치되어 있지 않습니다. PPTX 파싱을 건너뜁니다.")
        return ""

    try:
        prs = Presentation(io.BytesIO(file_bytes))
        slides = []
        for i, slide in enumerate(prs.slides, 1):
            texts = [
                shape.text_frame.text.strip()
                for shape in slide.shapes
                if shape.has_text_frame and shape.text_frame.text.strip()
            ]
            if texts:
                slides.append(f"[Slide {i}]\n" + "\n".join(texts))
        return "\n\n".join(slides)
    except Exception as e:
        logger.warning(f"PPTX 텍스트 추출 실패: {e}")
        return ""


def extract_docx_text(file_bytes: bytes) -> str:
    """
    DOCX 바이너리에서 텍스트를 추출합니다.

    Args:
        file_bytes: DOCX 파일의 바이너리 데이터

    Returns:
        추출된 텍스트 (단락별 구분)
    """
    try:
        from docx import Document
    except ImportError:
        logger.warning("python-docx가 설치되어 있지 않습니다. DOCX 파싱을 건너뜁니다.")
        return ""

    try:
        doc = Document(io.BytesIO(file_bytes))
        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)
    except Exception as e:
        logger.warning(f"DOCX 텍스트 추출 실패: {e}")
        return ""
