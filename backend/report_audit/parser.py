from collections.abc import Iterable
from datetime import date, datetime
from io import BytesIO
import re
from zipfile import ZipFile

from openpyxl import load_workbook
from pydantic import ValidationError
from pypdf import PdfReader
import xlrd

from .models import Draft

MAX_REPORT_BYTES = 1024 * 1024
MAX_REPORT_CHARS = 10_000
MAX_PDF_PAGES = 5
MAX_SHEET_ROWS = 64
MAX_SHEET_COLUMNS = 16
XLS_SIGNATURE = bytes.fromhex("D0 CF 11 E0 A1 B1 1A E1")
REPORT_TITLES = {"合成检验报告", "检验报告", "SYNTHETIC LAB REPORT"}
COLUMN_HEADERS = (
    {"CODE", "项目代码", "检验项目代码", "检验项目", "项目名称", "ITEM", "TEST"},
    {"VALUE", "数值", "结果"},
    {"UNIT", "单位"},
    {"DATE", "采样日期"},
)
PATIENT_LINE = re.compile(r"(?:PATIENT|患者编号)(?:\s*[:：]\s*|\s+)(\S+)", re.IGNORECASE)
ENCOUNTER_LINE = re.compile(r"(?:ENCOUNTER|就诊编号)(?:\s*[:：]\s*|\s+)(\S+)", re.IGNORECASE)


class InvalidReport(ValueError):
    pass


class PdfOcrRequired(InvalidReport):
    pass


def parse_report(text: str) -> Draft:
    if len(text) > MAX_REPORT_CHARS:
        raise InvalidReport("报告文本过长")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and lines[0].upper() in REPORT_TITLES:
        lines.pop(0)
    patient = PATIENT_LINE.fullmatch(lines[0]) if lines else None
    encounter = ENCOUNTER_LINE.fullmatch(lines[1]) if len(lines) >= 2 else None
    if not patient or not encounter:
        raise InvalidReport("报告缺少患者编号或就诊编号。请按示例在检验项目上方填写 PATIENT 和 ENCOUNTER")
    rows = [line.split() for line in lines[2:]]
    if rows and len(rows[0]) == 4 and all(cell.upper() in allowed for cell, allowed in zip(rows[0], COLUMN_HEADERS)):
        rows.pop(0)
    if not rows:
        raise InvalidReport("报告中没有检验项目")
    if any(len(row) != 4 for row in rows):
        raise InvalidReport("每行检验项目需包含项目代码、数值、单位和 YYYY-MM-DD 采样日期")
    try:
        return Draft.model_validate({
            "patient_id": patient[1], "encounter_id": encounter[1],
            "rows": [
                {"code": code, "value": value, "unit": unit, "collected_on": collected_on}
                for code, value, unit, collected_on in rows
            ],
        })
    except ValidationError as exc:
        raise InvalidReport("请核对编号、项目代码、数值、单位和采样日期，并移除重复项目") from exc


def _parse_pdf(data: bytes) -> Draft:
    try:
        pdf = PdfReader(BytesIO(data))
        if pdf.is_encrypted:
            raise InvalidReport("PDF 已加密，请上传未加密的副本")
        if len(pdf.pages) > MAX_PDF_PAGES:
            raise InvalidReport("PDF 须在 5 页以内")
        text = "\n".join(page.extract_text() for page in pdf.pages)
    except InvalidReport:
        raise
    except Exception as exc:
        raise InvalidReport("无法读取此 PDF，请检查文件能否正常打开") from exc
    if not text.strip():
        raise PdfOcrRequired("PDF 没有可提取的文字，请单独上传这份 PDF 进行扫描识别")
    try:
        return parse_report(text)
    except InvalidReport as first_error:
        # Some PDFs store table cells in separate text runs; layout joins cells on the same line.
        try:
            layout = "\n".join(page.extract_text(extraction_mode="layout") for page in pdf.pages)
            return parse_report(layout)
        except Exception:
            raise first_error from None


def _cell_text(cell: object) -> str:
    if cell is None:
        return ""
    if isinstance(cell, datetime):
        return cell.date().isoformat()
    if isinstance(cell, date):
        return cell.isoformat()
    return str(cell).strip()


def _parse_sheet(rows: Iterable[Iterable[object]]) -> Draft:
    lines = []
    for index, row in enumerate(rows, start=1):
        cells = [_cell_text(cell) for cell in row]
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        if index > MAX_SHEET_ROWS or len(cells) > MAX_SHEET_COLUMNS:
            raise InvalidReport("请将报告整理到首张工作表的前 64 行、前 16 列内")
        if any("\n" in cell or "\r" in cell for cell in cells):
            raise InvalidReport("Excel 单元格内不能包含换行，请按示例每行填写一项")
        first_cell = next((i for i, value in enumerate(cells) if value), 0)
        if not all(cells[first_cell:]):
            raise InvalidReport("Excel 有缺失的单元格，请核对项目代码、数值、单位和采样日期")
        lines.append(" ".join(cells[first_cell:]))
    if not lines:
        raise InvalidReport("Excel 的首张工作表为空")
    return parse_report("\n".join(lines))


def _parse_xlsx(data: bytes) -> Draft:
    with ZipFile(BytesIO(data)) as archive:
        if "xl/workbook.xml" not in archive.namelist():
            raise InvalidReport("此文件不是 Excel 工作簿，请另存为 .xlsx")
        if sum(entry.file_size for entry in archive.infolist()) > 20 * MAX_REPORT_BYTES:
            raise InvalidReport("Excel 工作簿解压后过大，请只保留报告所需的工作表")
    book = load_workbook(BytesIO(data), read_only=True, data_only=True, keep_links=False)
    try:
        if not book.worksheets:
            raise InvalidReport("Excel 中没有可读取的工作表")
        sheet = book.worksheets[0]
        if (sheet.max_row or 0) > MAX_SHEET_ROWS or (sheet.max_column or 0) > MAX_SHEET_COLUMNS:
            raise InvalidReport("请将报告整理到首张工作表的前 64 行、前 16 列内")
        return _parse_sheet(sheet.iter_rows(values_only=True))
    finally:
        book.close()


def _parse_xls(data: bytes) -> Draft:
    book = xlrd.open_workbook(file_contents=data, on_demand=True)
    try:
        if not book.nsheets:
            raise InvalidReport("Excel 中没有可读取的工作表")
        sheet = book.sheet_by_index(0)
        if sheet.nrows > MAX_SHEET_ROWS or sheet.ncols > MAX_SHEET_COLUMNS:
            raise InvalidReport("请将报告整理到首张工作表的前 64 行、前 16 列内")
        return _parse_sheet(
            (xlrd.xldate_as_datetime(cell.value, book.datemode) if cell.ctype == xlrd.XL_CELL_DATE else cell.value
             for cell in sheet.row(index))
            for index in range(sheet.nrows)
        )
    finally:
        book.release_resources()


def parse_report_file(data: bytes) -> Draft:
    if not data:
        raise InvalidReport("文件为空")
    if len(data) > MAX_REPORT_BYTES:
        raise InvalidReport("文件大小须在 1 MB 以内")
    if data.startswith(b"%PDF-"):
        return _parse_pdf(data)
    if data.startswith((b"PK\x03\x04", XLS_SIGNATURE)):
        try:
            return _parse_xls(data) if data.startswith(XLS_SIGNATURE) else _parse_xlsx(data)
        except InvalidReport:
            raise
        except Exception as exc:
            raise InvalidReport("无法读取此 Excel，请用 Excel 打开后另存为未加密的 .xlsx") from exc
    try:
        return parse_report(data.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise InvalidReport("请上传 Excel（.xlsx / .xls）、带文本层的 PDF 或 UTF-8 TXT") from exc
