"""Read scanned pages locally and compare structurally recognized results."""

from contextlib import contextmanager
from functools import cache
from hashlib import sha256
from io import BytesIO
from threading import Lock
from uuid import UUID, uuid4

import pypdfium2 as pdfium
from PIL import Image, ImageFilter, ImageOps
from pydantic import BaseModel, Field

from .evidence import EvidenceSource
from .parser import MAX_PDF_PAGES, MAX_REPORT_BYTES, InvalidReport
from .pdf_text import read_pdf_text_blocks
from .scan_analysis import ScanAnalysis, TableLayout, TextBlock, analyze_scan, group_rows, read_page_rows, reread_candidate_key

MAX_RENDER_SIDE = 2300
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_PHOTO_PIXELS = 24_000_000
LOW_CONFIDENCE = 0.82
MAX_REREADS_PER_PAGE = 24
_scan_slot = Lock()


class ScanBusy(RuntimeError):
    pass


class ScanLine(BaseModel):
    text: str
    uncertain: bool


class ScanPage(BaseModel):
    page: int
    lines: list[ScanLine]


class ScanPreview(BaseModel):
    pages: list[ScanPage]
    analysis: ScanAnalysis
    scan_id: UUID = Field(default_factory=uuid4)
    source_sha256: str


@cache
def _engine():
    from rapidocr_onnxruntime import RapidOCR

    # The package includes all three OCR models. No inference API or model download is needed.
    return RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1, max_side_len=MAX_RENDER_SIDE)


def _render_page(page, max_side: int):
    width, height = page.get_size()
    if not width > 0 or not height > 0:
        raise InvalidReport("PDF 中有无法读取的页面")
    bitmap = page.render(scale=min(3.2, max_side / max(width, height)))
    try:
        return bitmap.to_pil().convert("RGB")
    finally:
        bitmap.close()


def _reread_cell(image: Image.Image, field: str, unit: str, bounds: tuple[float, float, float, float]) -> str | None:
    width, height = image.size
    left, top, right, bottom = bounds
    box = (max(0, int(left * width) - 4), max(0, int(top * height) - 4),
           min(width, int(right * width) + 4), min(height, int(bottom * height) + 4))
    if box[2] - box[0] > 650 or box[3] - box[1] > 110:
        return None
    with image.crop(box) as crop, ImageOps.grayscale(crop) as gray:
        candidates: dict[object, list[tuple[float, str]]] = {}
        filters = (None, ImageFilter.MedianFilter(3), ImageFilter.MedianFilter(5), ImageFilter.GaussianBlur(0.65))
        for method in filters:
            processed = gray.filter(method) if method is not None else gray.copy()
            with processed, processed.resize((processed.width * 2, processed.height * 2), Image.Resampling.BICUBIC) as large:
                with ImageOps.expand(large, border=12, fill=255) as bordered, bordered.convert("RGB") as ready:
                    predicted, _ = _engine()(ready, use_det=False, use_rec=True, use_cls=False)
            if not predicted:
                continue
            text, score = predicted[0]
            key = reread_candidate_key(field, text, unit)
            # Require two different image preparations to agree, with at least one strong result.
            if key is not None and score >= 0.80:
                candidates.setdefault(key, []).append((float(score), text.strip()))
    if len(candidates) != 1:
        return None
    matches = next(iter(candidates.values()))
    confidence, text = max(matches)
    return text if len(matches) >= 2 and confidence >= 0.84 else None


def _read_page(page, columns: list[TableLayout]) -> tuple[float, list[list[TextBlock]], list[TableLayout]]:
    try:
        height, text = read_pdf_text_blocks(page, MAX_RENDER_SIDE)
    except pdfium.PdfiumError:
        text = []
    if text:
        rows, next_columns, count = read_page_rows(group_rows(text), height, columns)
        if count:
            return height, rows, next_columns
    with _render_page(page, MAX_RENDER_SIDE) as image:
        height, blocks = _image_blocks(image)
        remaining = MAX_REREADS_PER_PAGE

        def reread(field: str, unit: str, bounds: tuple[float, float, float, float]) -> str | None:
            nonlocal remaining
            if remaining <= 0:
                return None
            remaining -= 1
            return _reread_cell(image, field, unit, bounds)

        rows, next_columns, _ = read_page_rows(group_rows(blocks), height, columns, reread)
    return height, rows, next_columns


def _image_blocks(image: Image.Image) -> tuple[float, list[TextBlock]]:
    blocks, _ = _engine()(image, use_cls=True)
    image_width, image_height = image.size
    detected = []
    for box, content, score in (blocks or [])[:400]:
        if not content.strip():
            continue
        left, right = min(point[0] for point in box), max(point[0] for point in box)
        top, bottom = min(point[1] for point in box), max(point[1] for point in box)
        bounds = tuple(min(1.0, max(0.0, float(edge) / dimension)) for edge, dimension in (
            (left, image_width), (top, image_height), (right, image_width), (bottom, image_height),
        ))
        detected.append(TextBlock(content.strip()[:1000], float(score) < LOW_CONFIDENCE,
                                  float((left + right) / 2), float((top + bottom) / 2), float(bottom - top),
                                  bounds))
    return image_height, detected


@contextmanager
def _exclusive_scan():
    # Serialize calls to PDFium and the shared OCR engine.
    if not _scan_slot.acquire(blocking=False):
        raise ScanBusy("正在识别另一份报告，请稍后再试")
    try:
        yield
    finally:
        _scan_slot.release()


@contextmanager
def _scan_document(data: bytes):
    if len(data) > MAX_REPORT_BYTES:
        raise InvalidReport("PDF 文件大小须在 1 MB 以内")
    if not data.startswith(b"%PDF-"):
        raise InvalidReport("请选择一份 PDF 文件进行扫描识别")
    with _exclusive_scan():
        try:
            with pdfium.PdfDocument(data) as pdf:
                if len(pdf) > MAX_PDF_PAGES:
                    raise InvalidReport("扫描识别最多支持 5 页 PDF，请拆分后上传")
                yield pdf
        except pdfium.PdfiumError as exc:
            raise InvalidReport("无法打开扫描 PDF，请检查文件是否加密或损坏") from exc


def scan_pdf(
    data: bytes, *, source: EvidenceSource | None = None, include_source_audit: bool = True,
) -> ScanPreview:
    pages = []
    layout = []
    columns: list[TableLayout] = []
    with _scan_document(data) as pdf:
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                height, rows, columns = _read_page(page, columns)
                pages.append(ScanPage(page=index + 1, lines=[
                    ScanLine(text=block.text, uncertain=block.uncertain) for row in rows for block in row
                ]))
                layout.append((height, rows))
            finally:
                page.close()
    if not any(page.lines for page in pages):
        raise InvalidReport("没有识别出文字，请上传更清晰的扫描件")
    return ScanPreview(pages=pages, analysis=analyze_scan(layout, source=source, include_source_audit=include_source_audit),
                       source_sha256=sha256(data).hexdigest())


@contextmanager
def _open_photo(data: bytes, max_side: int):
    if len(data) > MAX_PHOTO_BYTES:
        raise InvalidReport("照片大小须在 8 MB 以内")
    try:
        with Image.open(BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as original:
            if original.width * original.height > MAX_PHOTO_PIXELS or getattr(original, "is_animated", False):
                raise InvalidReport("请选择 2400 万像素以内的单张报告照片")
            with ImageOps.exif_transpose(original) as rotated, rotated.convert("RGB") as image:
                image.thumbnail((max_side, max_side))
                yield image
    except (OSError, Image.DecompressionBombError) as exc:
        raise InvalidReport("请选择清晰的 JPG、PNG 或 WebP 报告照片") from exc


def scan_photo(data: bytes) -> ScanPreview:
    with _exclusive_scan(), _open_photo(data, MAX_RENDER_SIDE) as image:
        height, blocks = _image_blocks(image)
    if not blocks:
        raise InvalidReport("没有识别出文字，请上传更清晰的报告照片")
    page = ScanPage(page=1, lines=[ScanLine(text=block.text, uncertain=block.uncertain) for block in blocks])
    return ScanPreview(pages=[page], analysis=analyze_scan([(height, group_rows(blocks))], include_source_audit=False),
                       source_sha256=sha256(data).hexdigest())


def _jpeg(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, "JPEG", quality=88)
    return output.getvalue()


def scan_page_image(data: bytes, page_number: int, expected_sha256: str | None = None) -> bytes:
    if expected_sha256 is not None and sha256(data).hexdigest() != expected_sha256:
        raise InvalidReport("所选文件与扫描原件不一致，请选择原 PDF")
    with _scan_document(data) as pdf:
        if not 1 <= page_number <= len(pdf):
            raise InvalidReport("原 PDF 中没有这一页")
        page = pdf[page_number - 1]
        try:
            with _render_page(page, 1500) as image:
                return _jpeg(image)
        finally:
            page.close()


def scan_source_page(data: bytes, page_number: int, expected_sha256: str) -> bytes:
    if sha256(data).hexdigest() != expected_sha256:
        raise InvalidReport("所选文件与报告原件不一致，请重新选择")
    if data.startswith(b"%PDF-"):
        return scan_page_image(data, page_number)
    if page_number != 1:
        raise InvalidReport("这张报告照片只有一页")
    with _open_photo(data, 1500) as image:
        return _jpeg(image)
