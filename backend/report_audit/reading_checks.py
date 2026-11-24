"""CBC consistency checks using the values and precision printed on a report."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import re
from unicodedata import normalize

from pydantic import BaseModel, Field

from .scan_analysis import NUMBER, ScanFinding, _numeric_text


class ReadingCheck(BaseModel):
    title: str
    calculation: str
    conclusion: str
    rows: list[int]
    source_title: str
    source_url: str
    review_rows: list[int] = Field(default_factory=list)


_DIFFERENTIAL_URL = "https://www.beckmancoulter.com/download/file/wsr-293747/SOP_C41838AB?type=pdf"
_PLATELET_URL = "https://pmc.ncbi.nlm.nih.gov/articles/PMC6517618/"
_COUNT_UNITS = {
    "10^9/l", "x10^9/l", "10^3/ul", "x10^3/ul", "k/ul",
}


@dataclass(frozen=True)
class Measurement:
    row: int
    value: Decimal
    assumed_unit: bool = False

    @property
    def step(self) -> Decimal:
        return Decimal(1).scaleb(self.value.as_tuple().exponent)


def _measurement(row: int, field: ScanFinding, kind: str) -> Measurement | None:
    raw = _numeric_text(field.value)
    if len(raw) > 18 or not NUMBER.fullmatch(raw):
        return None
    value = Decimal(raw)
    ceiling = 100 if kind in {"percent", "plateletcrit"} else 1_000_000
    if not 0 <= value <= ceiling:
        return None
    unit = re.sub(r"(?<=10)\^?([⁹³])", lambda match: "^" + {"⁹": "9", "³": "3"}[match[1]], field.unit)
    unit = re.sub(r"\s+", "", normalize("NFKC", unit)).lower()
    unit = unit.replace("μ", "u").replace("×", "x").replace("*", "x")
    if kind == "count":
        if unit in _COUNT_UNITS:
            return Measurement(row, value)
        if "~" in unit and unit.replace("~", "^") in _COUNT_UNITS:
            return Measurement(row, value, assumed_unit=True)
    elif kind == "volume" and unit == "fl":
        return Measurement(row, value)
    elif kind in {"percent", "plateletcrit"} and unit == "%":
        return Measurement(row, value)
    elif kind == "plateletcrit" and not unit:
        return Measurement(row, value, assumed_unit=True)
    return None


def _estimate(value: Decimal, precision: str) -> str:
    return format(value.quantize(Decimal(precision), rounding=ROUND_HALF_UP).normalize(), "f")


def _range_at_printed_precision(value: Measurement) -> tuple[Decimal, Decimal]:
    half = value.step / 2
    return max(Decimal(0), value.value - half), value.value + half


def _consistent(left: Measurement, right: Measurement, reported: Measurement, divisor: int) -> bool:
    left_min, left_max = _range_at_printed_precision(left)
    right_min, right_max = _range_at_printed_precision(right)
    reported_min, reported_max = _range_at_printed_precision(reported)
    return left_min * right_min / divisor <= reported_max and left_max * right_max / divisor >= reported_min


def _cross_check(left: Measurement, right: Measurement, reported: Measurement, divisor: int) -> str:
    if not _consistent(left, right, reported, divisor):
        return "差异超过显示精度可解释的范围。请先核对原页数值、单位及采样信息。"
    if any(item.assumed_unit for item in (left, right, reported)):
        return "按所示单位假设，两者可在报告显示精度内对上。请到原页确认单位。"
    return "两者在报告显示精度内可以对上。"


def _review_rows(items: tuple[Measurement, ...], consistent: bool = True) -> list[int]:
    return [item.row for item in items if not consistent or item.assumed_unit]


def check_blood_counts(
    rows: list[ScanFinding], keys_by_row: list[tuple[str, ...]], primary: tuple[int, ...], evidence: set[int]
) -> list[ReadingCheck]:
    """Only pair unambiguous fields on one page, with same specimen as an explicit condition."""
    selected = {key for row in primary if row in evidence for key in keys_by_row[row]}

    def field(key: str, kind: str) -> Measurement | None:
        matches = [index for index, keys in enumerate(keys_by_row) if key in keys]
        if len(matches) != 1 or matches[0] not in evidence:
            return None
        return _measurement(matches[0], rows[matches[0]], kind)

    def same_page(*items: Measurement) -> bool:
        return len({rows[item.row].page for item in items}) == 1

    checks: list[ReadingCheck] = []
    if selected.intersection({"PLT", "MPV", "PCT", "PDW"}):
        plt, mpv, pct = field("PLT", "count"), field("MPV", "volume"), field("PCT", "plateletcrit")
        if plt is not None and mpv is not None and pct is not None and same_page(plt, mpv, pct):
            estimated = _estimate(plt.value * mpv.value / 10000, "0.0001")
            matches = _consistent(plt, mpv, pct, 10000)
            if matches and rows[plt.row].status == "NORMAL" and rows[mpv.row].status == rows[pct.row].status == "LOW":
                interpretation = "血小板计数仍在范围内，平均体积偏小与压积偏低在数值上相符。PDW 衡量大小差异，不参与这项计算。"
            else:
                interpretation = ""
            checks.append(ReadingCheck(
                title="血小板压积交叉核验",
                calculation=f"若为同次采样，PLT {plt.value:f}（×10⁹/L） × MPV {mpv.value:f} fL ÷ 10,000"
                            f" ≈ {estimated}%。报告 PCT 为 {pct.value:f}"
                            f"{'（按 % 试算）' if pct.assumed_unit else '%'}。",
                conclusion=_cross_check(plt, mpv, pct, 10000) + interpretation,
                rows=[plt.row, mpv.row, pct.row], source_title="血小板体积指标研究", source_url=_PLATELET_URL,
                review_rows=_review_rows((plt, mpv, pct), matches),
            ))

    wbc = field("WBC", "count")
    for prefix, name in (("BAS", "嗜碱性粒细胞"), ("LYM", "淋巴细胞"), ("NEUT", "中性粒细胞"),
                         ("EOS", "嗜酸性粒细胞"), ("MONO", "单核细胞")):
        if not selected.intersection({prefix + "%", prefix + "#"}):
            continue
        pct, count = field(prefix + "%", "percent"), field(prefix + "#", "count")
        if pct is None:
            continue
        if wbc is not None and same_page(wbc, pct):
            estimated = _estimate(wbc.value * pct.value / 100, "0.0001")
            calculation = f"若为同次采样，WBC {wbc.value:f}（×10⁹/L） × {prefix}% {pct.value:f}% ≈ {estimated} ×10⁹/L。"
            conclusion = "这是估算值。请查看同次报告中的绝对计数和参考范围。"
            used = [wbc.row, pct.row]
            review_rows = _review_rows((wbc, pct))
            if count is not None and same_page(wbc, count):
                calculation += f"报告 {prefix}# 为 {count.value:f}（×10⁹/L）。"
                conclusion = _cross_check(wbc, pct, count, 100)
                used.append(count.row)
                review_rows = _review_rows((wbc, pct, count), _consistent(wbc, pct, count, 100))
            elif wbc.assumed_unit:
                conclusion += " 请先到原页确认白细胞单位。"
        elif count is not None and count.value > 0 and pct.value > 0 and same_page(pct, count):
            implied = _estimate(count.value * 100 / pct.value, "0.01")
            calculation = (f"若为同次采样，{prefix}# {count.value:f}（×10⁹/L） ÷ {prefix}% {pct.value:f}%"
                           f" ≈ {implied} ×10⁹/L，为反推的白细胞总数。")
            conclusion = "在相同比例下，白细胞总数越少，绝对计数就越低。请用原报告同次 WBC 核对反推值。"
            if count.assumed_unit:
                conclusion += " 请先到原页确认计数单位。"
            used = [count.row, pct.row]
            review_rows = _review_rows((count, pct))
        else:
            continue
        checks.append(ReadingCheck(
            title=f"{name}比例与绝对计数",
            calculation=calculation, conclusion=conclusion, rows=used,
            source_title="Beckman Coulter · 白细胞分类计算", source_url=_DIFFERENTIAL_URL,
            review_rows=review_rows,
        ))
    return checks
