"""Recover positioned table text from PDFs with a native text layer."""

from statistics import median

import pypdfium2 as pdfium

from .scan_analysis import TextBlock, group_rows

MAX_TEXT_CHARACTERS = 25_000


def _contains_page_image(page: pdfium.PdfPage, width: float, height: float) -> bool:
    for image in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]):
        left, bottom, right, top = image.get_bounds()
        if (right - left) * (top - bottom) >= 0.45 * width * height:
            return True
    return False


def _combine_run(run: list[TextBlock], width: float, height: float) -> TextBlock:
    left, top, right, bottom = zip(*(char.bounds for char in run if char.bounds is not None))
    bounds = (min(left), min(top), max(right), max(bottom))
    return TextBlock("".join(char.text for char in run), False, (bounds[0] + bounds[2]) * width / 2,
                     (bounds[1] + bounds[3]) * height / 2, (bounds[3] - bounds[1]) * height, bounds)


def read_pdf_text_blocks(page: pdfium.PdfPage, max_side: int) -> tuple[float, list[TextBlock]]:
    """Keep scan coordinates compatible with the page renderer and its highlight boxes."""
    page_width, page_height = page.get_size()
    if page.get_rotation() or min(page_width, page_height) <= 0:
        return 0, []
    scale = min(3.2, max_side / max(page_width, page_height))
    width, height = page_width * scale, page_height * scale
    text = page.get_textpage()
    try:
        count = text.count_chars()
        if not 0 < count <= MAX_TEXT_CHARACTERS or _contains_page_image(page, page_width, page_height):
            return height, []
        origin_x, _, _, origin_top = page.get_bbox()
        glyphs = []
        for index in range(count):
            code = pdfium.raw.FPDFText_GetUnicode(text, index)
            if not 0 < code <= 0x10FFFF:
                continue
            char = chr(code)
            if char.isspace() or char == "\ufffd":
                continue
            left, bottom, right, top = text.get_charbox(index, loose=True)
            l, r = (left - origin_x) / page_width, (right - origin_x) / page_width
            t, b = (origin_top - top) / page_height, (origin_top - bottom) / page_height
            if not 0 <= l < r <= 1 or not 0 <= t < b <= 1:
                continue
            glyphs.append(TextBlock(char, False, (l + r) * width / 2, (t + b) * height / 2, (b - t) * height, (l, t, r, b)))
    finally:
        text.close()
    blocks = []
    for row in group_rows(glyphs):
        gap = max(3.0, median(char.height for char in row) * 0.85) / width
        run: list[TextBlock] = []
        for glyph in row:
            if run and glyph.bounds[0] - run[-1].bounds[2] > gap:
                blocks.append(_combine_run(run, width, height))
                run = []
            run.append(glyph)
        if run:
            blocks.append(_combine_run(run, width, height))
    return height, blocks
