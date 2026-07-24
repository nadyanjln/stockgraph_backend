"""PDF extraction entry points for financial reports.

The active implementation is PaddleOCR-based. This module keeps the historical
function names used by the ingestion pipeline.
"""

from __future__ import annotations

from app.services.paddleocr_service import (
    MIN_PAGE_TEXT_CHARS,
    PaddleOcrService,
    PdfExtractionResult,
    PdfPageExtraction,
    clean_pdf_text,
    combine_page_text,
    extract_pdf_documents,
    extract_pdf_pages,
    table_rows_to_markdown,
)

__all__ = [
    "MIN_PAGE_TEXT_CHARS",
    "PaddleOcrService",
    "PdfExtractionResult",
    "PdfPageExtraction",
    "clean_pdf_text",
    "combine_page_text",
    "extract_pdf_documents",
    "extract_pdf_pages",
    "table_rows_to_markdown",
]
