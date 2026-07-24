"""PaddleOCR document extraction service for financial report PDFs."""

from __future__ import annotations

import os
import re
import tempfile
import time
import inspect
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.utils.logger import get_logger

logger = get_logger("stockgraph.pdf")

MIN_PAGE_TEXT_CHARS = 80
LOW_CONFIDENCE_THRESHOLD = 0.55
EXTRACTION_METHOD = "paddleocr"

PipelineFactory = Callable[[], Any]


@dataclass
class PdfPageExtraction:
    text: str
    page_number: int
    source_file: str
    extraction_method: str = EXTRACTION_METHOD
    document_type: str = "financial_report"
    document_year: int | None = None
    document_period: str = ""
    ticker: str = ""
    company: str = ""
    ocr_confidence: float | None = None
    needs_review: bool = False
    extraction_warning: str = ""
    tables: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PdfExtractionResult:
    source_file: str
    pages: list[PdfPageExtraction] = field(default_factory=list)
    error: str = ""

    @property
    def text(self) -> str:
        return combine_page_text(self.pages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "pages": [page.to_dict() for page in self.pages],
            "error": self.error,
        }


class PaddleOcrService:
    """Lazy singleton wrapper around PaddleOCR's PP-StructureV3 pipeline.

    The service accepts PDFs as paths or bytes and normalizes PaddleOCR output
    into page-level text, table markdown, and citation metadata.
    """

    def __init__(
        self,
        *,
        pipeline_factory: PipelineFactory | None = None,
        min_chars: int = MIN_PAGE_TEXT_CHARS,
        low_confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._pipeline_factory = pipeline_factory
        self._pipeline: Any | None = None
        self._min_chars = min_chars
        self._low_confidence_threshold = low_confidence_threshold

    def extract_pdf_pages(
        self,
        pdf_source: str | bytes,
        *,
        source_file: str = "",
        document_type: str = "financial_report",
        document_year: int | None = None,
        document_period: str = "",
        ticker: str = "",
        company: str = "",
        min_chars: int | None = None,
    ) -> PdfExtractionResult:
        resolved_source = _source_name(pdf_source, source_file)
        started_at = time.perf_counter()
        logger.info("Processing PDF %s with PaddleOCR", resolved_source)

        tmp_path = ""
        try:
            input_path, tmp_path = _prepare_pdf_input(pdf_source, resolved_source)
            pipeline = self._get_pipeline()
            raw_results = list(_iter_pipeline_results(pipeline.predict(input_path)))
        except Exception as exc:  # noqa: BLE001 - callers may batch many PDFs.
            logger.error("PaddleOCR failed for PDF %s: %s", resolved_source, exc)
            _cleanup_temp_file(tmp_path)
            return PdfExtractionResult(source_file=resolved_source, error=str(exc))
        finally:
            if "tmp_path" in locals():
                _cleanup_temp_file(tmp_path)

        pages: list[PdfPageExtraction] = []
        warning_pages = 0
        total_tables = 0
        effective_min_chars = self._min_chars if min_chars is None else min_chars

        if not raw_results:
            warning_pages = 1
            pages.append(
                PdfPageExtraction(
                    text="",
                    page_number=1,
                    source_file=resolved_source,
                    document_type=document_type,
                    document_year=document_year,
                    document_period=document_period,
                    ticker=ticker,
                    company=company,
                    ocr_confidence=None,
                    needs_review=True,
                    extraction_warning="PaddleOCR returned no page output",
                )
            )

        for index, item in enumerate(raw_results, start=1):
            try:
                page_data = _normalize_result(item)
                page_number = _page_number(page_data, index)
                text, confidence, tables, warnings = _extract_page_content(page_data)
                text = clean_pdf_text(text)
                table_text = "\n\n".join(table["markdown"] for table in tables)
                page_text = _join_page_text(text, table_text)
                needs_review = _needs_review(
                    text,
                    confidence,
                    warnings,
                    min_chars=effective_min_chars,
                    low_confidence_threshold=self._low_confidence_threshold,
                )
                warning = "; ".join(warnings)
                if needs_review and not warning:
                    warning = "PaddleOCR returned insufficient content"
                if needs_review:
                    warning_pages += 1
                total_tables += len(tables)

                pages.append(
                    PdfPageExtraction(
                        text=page_text,
                        page_number=page_number,
                        source_file=resolved_source,
                        document_type=document_type,
                        document_year=document_year,
                        document_period=document_period,
                        ticker=ticker,
                        company=company,
                        ocr_confidence=confidence,
                        needs_review=needs_review,
                        extraction_warning=warning,
                        tables=tables,
                    )
                )
                logger.info(
                    "PaddleOCR page extracted; file=%s page=%s chars=%s tables=%s confidence=%s review=%s",
                    resolved_source,
                    page_number,
                    len(text),
                    len(tables),
                    "-" if confidence is None else f"{confidence:.3f}",
                    needs_review,
                )
            except Exception as exc:  # noqa: BLE001 - keep processing pages.
                warning_pages += 1
                page_number = index
                logger.warning(
                    "PaddleOCR page parsing failed; file=%s page=%s error=%s",
                    resolved_source,
                    page_number,
                    exc,
                )
                pages.append(
                    PdfPageExtraction(
                        text="",
                        page_number=page_number,
                        source_file=resolved_source,
                        document_type=document_type,
                        document_year=document_year,
                        document_period=document_period,
                        ticker=ticker,
                        company=company,
                        ocr_confidence=None,
                        needs_review=True,
                        extraction_warning="PaddleOCR page parsing failed",
                        error=str(exc),
                    )
                )

        elapsed = time.perf_counter() - started_at
        logger.info(
            "PaddleOCR extraction summary; file=%s pages=%s extracted=%s warning_pages=%s tables=%s elapsed=%.2fs",
            resolved_source,
            len(raw_results) or len(pages),
            len(pages),
            warning_pages,
            total_tables,
            elapsed,
        )
        return PdfExtractionResult(source_file=resolved_source, pages=pages)

    def _get_pipeline(self) -> Any:
        if self._pipeline is not None:
            logger.debug("Using cached PaddleOCR pipeline instance")
            return self._pipeline

        logger.info("Initializing PaddleOCR PP-StructureV3 pipeline")
        if self._pipeline_factory is not None:
            self._pipeline = self._pipeline_factory()
            return self._pipeline

        try:
            from paddleocr import PPStructureV3
        except ImportError as exc:
            raise RuntimeError(
                "PaddleOCR is not installed. Install paddleocr>=3.3.0 and a compatible PaddlePaddle runtime."
            ) from exc

        kwargs: dict[str, Any] = {}
        lang = os.getenv("STOCKGRAPH_PADDLEOCR_LANG", "en").strip()
        if lang:
            kwargs["lang"] = lang
        device = os.getenv("STOCKGRAPH_PADDLEOCR_DEVICE", "").strip()
        if device:
            kwargs["device"] = device
        kwargs.update(
            {
                "use_doc_orientation_classify": _env_bool(
                    "STOCKGRAPH_PADDLEOCR_USE_DOC_ORIENTATION", True,
                ),
                "use_doc_unwarping": _env_bool(
                    "STOCKGRAPH_PADDLEOCR_USE_DOC_UNWARPING", False,
                ),
                "use_textline_orientation": _env_bool(
                    "STOCKGRAPH_PADDLEOCR_USE_TEXTLINE_ORIENTATION", True,
                ),
                "use_table_recognition": _env_bool(
                    "STOCKGRAPH_PADDLEOCR_USE_TABLE_RECOGNITION", True,
                ),
                "use_formula_recognition": _env_bool(
                    "STOCKGRAPH_PADDLEOCR_USE_FORMULA_RECOGNITION", False,
                ),
            }
        )
        self._pipeline = PPStructureV3(**_filter_supported_kwargs(PPStructureV3, kwargs))
        return self._pipeline


_default_service: PaddleOcrService | None = None


def get_default_service() -> PaddleOcrService:
    global _default_service
    if _default_service is None:
        _default_service = PaddleOcrService()
    return _default_service


def extract_pdf_pages(
    pdf_source: str | bytes,
    *,
    source_file: str = "",
    document_type: str = "financial_report",
    document_year: int | None = None,
    document_period: str = "",
    ticker: str = "",
    company: str = "",
    min_chars: int = MIN_PAGE_TEXT_CHARS,
    ocr_service: PaddleOcrService | None = None,
) -> PdfExtractionResult:
    service = ocr_service or get_default_service()
    return service.extract_pdf_pages(
        pdf_source,
        source_file=source_file,
        document_type=document_type,
        document_year=document_year,
        document_period=document_period,
        ticker=ticker,
        company=company,
        min_chars=min_chars,
    )


def extract_pdf_documents(
    pdf_sources: list[str | bytes],
    *,
    ocr_service: PaddleOcrService | None = None,
) -> list[PdfExtractionResult]:
    service = ocr_service or get_default_service()
    return [
        service.extract_pdf_pages(pdf_source)
        for pdf_source in pdf_sources
    ]


def combine_page_text(pages: list[PdfPageExtraction]) -> str:
    chunks: list[str] = []
    for page in pages:
        if not page.text.strip():
            continue
        header_parts = [
            f"source_file={page.source_file}",
            f"page_number={page.page_number}",
            f"extraction_method={page.extraction_method}",
            f"document_type={page.document_type}",
        ]
        if page.document_year:
            header_parts.append(f"document_year={page.document_year}")
        if page.document_period:
            header_parts.append(f"document_period={page.document_period}")
        if page.ticker:
            header_parts.append(f"ticker={page.ticker}")
        if page.company:
            header_parts.append(f"company={page.company}")
        if page.ocr_confidence is not None:
            header_parts.append(f"ocr_confidence={page.ocr_confidence:.3f}")
        chunks.append(f"[{'; '.join(header_parts)}]\n{page.text.strip()}")
    return "\n\n".join(chunks)


def clean_pdf_text(text: str) -> str:
    text = text.replace("\x00", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    compact_lines: list[str] = []
    blank_seen = False
    for line in lines:
        if not line:
            if not blank_seen and compact_lines:
                compact_lines.append("")
            blank_seen = True
            continue
        compact_lines.append(line)
        blank_seen = False
    return "\n".join(compact_lines).strip()


def table_rows_to_markdown(rows: list[list[Any]]) -> str:
    cleaned_rows = [
        [clean_pdf_text("" if cell is None else str(cell)) for cell in row]
        for row in rows
        if row
    ]
    cleaned_rows = [row for row in cleaned_rows if any(cell for cell in row)]
    if not cleaned_rows:
        return ""

    width = max(len(row) for row in cleaned_rows)
    normalized = [row + [""] * (width - len(row)) for row in cleaned_rows]
    header = normalized[0]
    separator = ["---"] * width
    body = normalized[1:]
    markdown_rows = [header, separator, *body]
    return "\n".join(
        "| " + " | ".join(_escape_markdown_cell(cell) for cell in row) + " |"
        for row in markdown_rows
    )


def _extract_page_content(page_data: dict[str, Any]) -> tuple[str, float | None, list[dict[str, Any]], list[str]]:
    res = _result_payload(page_data)
    blocks = _collect_layout_blocks(res)
    tables = _collect_tables(res)
    warnings: list[str] = []

    if blocks:
        ordered = sorted(blocks, key=lambda item: _bbox_sort_key(item.get("bbox")))
        text_parts = [str(item.get("text") or "").strip() for item in ordered]
        text = "\n".join(part for part in text_parts if part)
    else:
        text = _collect_ocr_text(res)

    confidence = _confidence(res)
    if tables and not any(_is_table_block(block) for block in blocks):
        warnings.append("PaddleOCR table extraction produced table output without layout table block")
    if not tables and _has_table_hint(res):
        warnings.append("PaddleOCR table extraction did not return structured table content")
    if _invalid_character_ratio(text) > 0.2:
        warnings.append("PaddleOCR text contains many non-standard characters")

    return text, confidence, tables, warnings


def _collect_layout_blocks(res: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    for key in (
        "parsing_res_list",
        "layout_parsing_result",
        "layout_res",
        "layout_result",
        "blocks",
    ):
        value = res.get(key)
        if isinstance(value, list):
            candidates.extend(value)

    blocks: list[dict[str, Any]] = []
    for raw in candidates:
        block = _to_dict(raw)
        label = str(
            block.get("block_label")
            or block.get("label")
            or block.get("type")
            or block.get("category")
            or ""
        )
        text = str(
            block.get("block_content")
            or block.get("content")
            or block.get("markdown")
            or block.get("text")
            or block.get("html")
            or ""
        ).strip()
        if not text:
            continue
        blocks.append({
            "label": label,
            "text": clean_pdf_text(text),
            "bbox": _first_present(block, "bbox", "box", "coordinate", "poly", "points"),
        })
    return blocks


def _collect_ocr_text(res: dict[str, Any]) -> str:
    for key in ("markdown", "markdown_text", "text", "content"):
        value = res.get(key)
        if isinstance(value, str) and value.strip():
            return value

    ocr_res = _to_dict(res.get("overall_ocr_res") or res.get("ocr_res") or {})
    texts = ocr_res.get("rec_texts") or ocr_res.get("texts") or res.get("rec_texts")
    boxes = ocr_res.get("rec_boxes") or ocr_res.get("dt_polys") or res.get("rec_boxes")
    if isinstance(texts, list):
        indexed = []
        for index, text in enumerate(texts):
            bbox = boxes[index] if isinstance(boxes, list) and index < len(boxes) else None
            indexed.append((_bbox_sort_key(bbox), str(text)))
        return "\n".join(text for _, text in sorted(indexed) if text.strip())

    generic = _collect_strings(res)
    return "\n".join(generic[:200])


def _collect_tables(res: dict[str, Any]) -> list[dict[str, Any]]:
    raw_tables: list[Any] = []
    for key in (
        "table_res_list",
        "table_result",
        "tables",
        "table_cells_result",
    ):
        value = res.get(key)
        if isinstance(value, list):
            raw_tables.extend(value)
        elif isinstance(value, dict):
            raw_tables.append(value)

    tables: list[dict[str, Any]] = []
    for table_index, raw in enumerate(raw_tables, start=1):
        table = _to_dict(raw)
        markdown = _table_markdown(table)
        if not markdown:
            continue
        tables.append({
            "page_number": _page_number(res, 1),
            "table_index": table_index,
            "extraction_method": EXTRACTION_METHOD,
            "ocr_confidence": _confidence(table),
            "markdown": markdown,
        })
    return tables


def _table_markdown(table: dict[str, Any]) -> str:
    for key in ("markdown", "markdown_text"):
        value = table.get(key)
        if isinstance(value, str) and value.strip():
            return clean_pdf_text(value)

    rows = table.get("rows") or table.get("cells") or table.get("table")
    if isinstance(rows, list) and rows and all(isinstance(row, list) for row in rows):
        return table_rows_to_markdown(rows)

    for key in ("pred_html", "html", "table_html"):
        value = table.get(key)
        if isinstance(value, str) and value.strip():
            return clean_pdf_text(value)

    text = table.get("text") or table.get("content")
    return clean_pdf_text(str(text)) if text else ""


def _confidence(res: dict[str, Any]) -> float | None:
    values: list[float] = []
    for key in ("confidence", "ocr_confidence", "score"):
        value = res.get(key)
        if isinstance(value, (int, float)):
            values.append(float(value))

    ocr_res = _to_dict(res.get("overall_ocr_res") or res.get("ocr_res") or {})
    for key in ("rec_scores", "scores", "confidence"):
        value = ocr_res.get(key) or res.get(key)
        if isinstance(value, list):
            values.extend(float(item) for item in value if isinstance(item, (int, float)))
        elif isinstance(value, (int, float)):
            values.append(float(value))

    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _normalize_result(item: Any) -> dict[str, Any]:
    data = _to_dict(item)
    if not data:
        raise ValueError("PaddleOCR returned an empty page result")
    return data


def _result_payload(page_data: dict[str, Any]) -> dict[str, Any]:
    res = page_data.get("res")
    if isinstance(res, dict):
        return res
    return page_data


def _to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "json"):
        try:
            data = value.json
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "res"):
        try:
            data = value.res
            if isinstance(data, dict):
                return {"res": data}
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "to_dict"):
        try:
            data = value.to_dict()
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _page_number(page_data: dict[str, Any], fallback: int) -> int:
    for key in ("page_number", "page_num", "page_id"):
        value = page_data.get(key)
        if isinstance(value, int) and value > 0:
            return value
    for key in ("page_index", "page_idx"):
        value = page_data.get(key)
        if isinstance(value, int) and value >= 0:
            return value + 1
    return fallback


def _iter_pipeline_results(value: Any) -> Iterable[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return [value]
    return value


def _needs_review(
    text: str,
    confidence: float | None,
    warnings: list[str],
    *,
    min_chars: int,
    low_confidence_threshold: float,
) -> bool:
    if len(text.strip()) < min_chars:
        return True
    if confidence is not None and confidence < low_confidence_threshold:
        return True
    return bool(warnings)


def _prepare_pdf_input(pdf_source: str | bytes, source_file: str) -> tuple[str, str]:
    if isinstance(pdf_source, str):
        return pdf_source, ""
    suffix = Path(source_file).suffix if Path(source_file).suffix.lower() == ".pdf" else ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(pdf_source)
        return tmp.name, tmp.name


def _cleanup_temp_file(tmp_path: str) -> None:
    if not tmp_path:
        return
    try:
        Path(tmp_path).unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove temporary OCR file %s", tmp_path)


def _source_name(pdf_source: str | bytes, source_file: str) -> str:
    if source_file:
        return source_file
    if isinstance(pdf_source, str):
        return Path(pdf_source).name
    return "<bytes>"


def _join_page_text(text: str, table_text: str) -> str:
    if text and table_text:
        return f"{text}\n\n### Tables detected by PaddleOCR\n{table_text}"
    return text or table_text


def _escape_markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _bbox_sort_key(bbox: Any) -> tuple[float, float]:
    points = _flatten_numbers(bbox)
    if len(points) >= 2:
        xs = points[0::2]
        ys = points[1::2]
        return (min(ys), min(xs))
    return (0.0, 0.0)


def _flatten_numbers(value: Any) -> list[float]:
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, (list, tuple)):
        numbers: list[float] = []
        for item in value:
            numbers.extend(_flatten_numbers(item))
        return numbers
    return []


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


def _is_table_block(block: dict[str, Any]) -> bool:
    return "table" in str(block.get("label", "")).lower()


def _has_table_hint(res: dict[str, Any]) -> bool:
    text = " ".join(str(key).lower() for key in res.keys())
    return "table" in text


def _invalid_character_ratio(text: str) -> float:
    if not text:
        return 0.0
    invalid = sum(1 for char in text if not (char.isprintable() or char in "\n\r\t"))
    return invalid / max(1, len(text))


def _collect_strings(value: Any) -> list[str]:
    output: list[str] = []
    if isinstance(value, str):
        clean = value.strip()
        if clean:
            output.append(clean)
    elif isinstance(value, dict):
        for item in value.values():
            output.extend(_collect_strings(item))
    elif isinstance(value, list):
        for item in value:
            output.extend(_collect_strings(item))
    return output


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _filter_supported_kwargs(callable_obj: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return kwargs
    params = signature.parameters.values()
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params):
        return kwargs
    allowed = set(signature.parameters)
    dropped = sorted(set(kwargs) - allowed)
    if dropped:
        logger.info(
            "PaddleOCR PP-StructureV3 does not expose constructor options: %s",
            ", ".join(dropped),
        )
    return {key: value for key, value in kwargs.items() if key in allowed}


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
