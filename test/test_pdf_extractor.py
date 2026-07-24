from __future__ import annotations

from pathlib import Path

from app.services.paddleocr_service import PaddleOcrService, combine_page_text
from app.services.pdf_extractor import extract_pdf_documents, extract_pdf_pages


class FakePaddlePipeline:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def predict(self, _input_path: str):
        item = self.outputs[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def _service(*outputs) -> PaddleOcrService:
    pipeline = FakePaddlePipeline(outputs)
    return PaddleOcrService(pipeline_factory=lambda: pipeline)


def _ocr_page(page_index: int, texts: list[str], scores: list[float] | None = None):
    return {
        "page_index": page_index,
        "res": {
            "overall_ocr_res": {
                "rec_texts": texts,
                "rec_scores": scores or [0.96 for _ in texts],
                "rec_boxes": [
                    [[40, 20 + idx * 30], [400, 20 + idx * 30], [400, 40 + idx * 30], [40, 40 + idx * 30]]
                    for idx, _ in enumerate(texts)
                ],
            }
        },
    }


def test_multi_page_pdf_extracts_with_paddleocr_and_page_metadata():
    result = extract_pdf_pages(
        b"%PDF fake",
        source_file="bbca-2024.pdf",
        document_year=2024,
        ticker="BBCA",
        company="Bank Central Asia",
        ocr_service=_service([
            _ocr_page(0, ["Laporan Keuangan BBCA 2024", "Total aset 1.234.567"]),
            _ocr_page(1, ["Catatan laporan keuangan", "Laba bersih 123.456"]),
        ]),
    )

    assert result.error == ""
    assert [page.page_number for page in result.pages] == [1, 2]
    assert all(page.extraction_method == "paddleocr" for page in result.pages)
    assert all(page.source_file == "bbca-2024.pdf" for page in result.pages)
    assert result.pages[0].document_year == 2024
    assert result.pages[0].ticker == "BBCA"
    assert "Total aset" in result.pages[0].text


def test_text_layout_pdf_extracts_metadata_and_confidence():
    result = extract_pdf_pages(
        b"%PDF fake",
        source_file="tlkm-2025.pdf",
        document_period="Tahunan",
        ocr_service=_service([
            _ocr_page(
                0,
                [
                    "Laporan Tahunan TLKM",
                    "Pendapatan, laba bersih, aset, ekuitas, dan catatan keuangan tersedia lengkap untuk analisis.",
                ],
                [0.91, 0.93],
            )
        ]),
    )

    page = result.pages[0]
    assert page.extraction_method == "paddleocr"
    assert page.document_type == "financial_report"
    assert page.document_period == "Tahunan"
    assert page.ocr_confidence == 0.92
    assert page.needs_review is False


def test_short_ocr_result_is_marked_for_review_without_crashing():
    result = extract_pdf_pages(
        b"%PDF fake",
        source_file="scan-like.pdf",
        ocr_service=_service([_ocr_page(0, ["A"], [0.99])]),
    )

    assert len(result.pages) == 1
    assert result.pages[0].needs_review is True
    assert result.pages[0].extraction_warning == "PaddleOCR returned insufficient content"
    assert result.pages[0].extraction_method == "paddleocr"


def test_table_extraction_adds_markdown_to_page_output():
    result = extract_pdf_pages(
        b"%PDF fake",
        source_file="table.pdf",
        ocr_service=_service([
            {
                "page_index": 0,
                "res": {
                    "parsing_res_list": [
                        {
                            "block_label": "text",
                            "block_content": "Ikhtisar keuangan tahunan dengan pendapatan, laba bersih, aset, ekuitas, dan arus kas operasi.",
                            "bbox": [10, 10, 300, 40],
                        },
                        {
                            "block_label": "table",
                            "block_content": "Tabel metrik keuangan utama untuk mempertahankan konteks struktur laporan.",
                            "bbox": [10, 50, 500, 200],
                        },
                    ],
                    "table_res_list": [
                        {
                            "rows": [["Metric", "2024"], ["Revenue", "1000"]],
                            "confidence": 0.88,
                        }
                    ],
                    "overall_ocr_res": {"rec_scores": [0.9, 0.88]},
                },
            }
        ]),
    )

    page = result.pages[0]
    assert page.needs_review is False
    assert page.tables
    assert "| Metric | 2024 |" in page.tables[0]["markdown"]
    assert "### Tables detected by PaddleOCR" in page.text


def test_page_level_parse_error_keeps_following_pages():
    result = extract_pdf_pages(
        b"%PDF fake",
        source_file="partial.pdf",
        ocr_service=_service([object(), _ocr_page(1, ["Halaman kedua berhasil diproses " * 4])]),
    )

    assert len(result.pages) == 2
    assert result.pages[0].needs_review is True
    assert result.pages[0].error
    assert result.pages[1].page_number == 2
    assert "Halaman kedua" in result.pages[1].text


def test_citation_metadata_survives_combined_text():
    result = extract_pdf_pages(
        b"%PDF fake",
        source_file="idx-cdn.pdf",
        ticker="BBNI",
        ocr_service=_service([_ocr_page(0, ["Laporan keuangan BBNI " * 5])]),
    )

    combined = combine_page_text(result.pages)

    assert "source_file=idx-cdn.pdf" in combined
    assert "page_number=1" in combined
    assert "extraction_method=paddleocr" in combined
    assert "ticker=BBNI" in combined


def test_broken_pdf_does_not_stop_batch_ingestion():
    result = extract_pdf_documents(
        [b"not a pdf", b"%PDF good"],
        ocr_service=_service(
            RuntimeError("invalid pdf"),
            [_ocr_page(0, ["Valid OCR financial report text " * 8])],
        ),
    )

    assert result[0].error
    assert result[1].error == ""
    assert result[1].pages[0].extraction_method == "paddleocr"


def test_pdf_extraction_pipeline_has_no_pymupdf_imports():
    root = Path(__file__).resolve().parents[1]
    extractor_source = (root / "app" / "services" / "pdf_extractor.py").read_text(encoding="utf-8")
    service_source = (root / "app" / "services" / "paddleocr_service.py").read_text(encoding="utf-8")

    assert "fitz" not in extractor_source
    assert "fitz" not in service_source
    assert "pymupdf" not in extractor_source.lower()
    assert "pymupdf" not in service_source.lower()
