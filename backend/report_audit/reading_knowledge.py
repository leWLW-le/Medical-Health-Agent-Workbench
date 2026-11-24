"""Reviewed reference notes for interpreting uploaded laboratory reports."""

from dataclasses import dataclass
from functools import lru_cache
import os
import re
import threading
from unicodedata import normalize

from langchain_classic.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from langchain_core.vectorstores import InMemoryVectorStore

from rag.embeddings import _hf_model_is_cached, get_embeddings


@dataclass(frozen=True)
class Note:
    id: str
    keys: tuple[str, ...]
    title: str
    url: str
    text: str


_MEDLINE = "https://medlineplus.gov/lab-tests/"
_LIMIT = Note(
    "reference_ranges", (), "MedlinePlus · 如何理解检验结果", _MEDLINE + "how-to-understand-your-lab-results/",
    "超出报告参考范围提供的是一条线索。判断它对个人意味着什么，还要结合症状、病史和既往结果。",
)
_NOTES = (
    Note("basophils", ("BAS%", "BAS#", "BAS"), "MedlinePlus · 白细胞分类", _MEDLINE + "blood-differential/",
         "BAS% 表示嗜碱性粒细胞占白细胞的比例，BAS# 表示它的绝对计数。"
         "比例偏高时应查看绝对计数和白细胞总数。单凭比例偏高无法判断有无过敏，也无法推断过敏反应的强度。"),
    Note("differential", ("WBC", "EOS%", "EOS#", "NEUT%", "NEUT#", "MONO%", "MONO#"),
         "MedlinePlus · 白细胞分类", _MEDLINE + "blood-differential/",
         "白细胞总数反映总体数量，白细胞分类分别报告各类细胞的比例或计数。解读某一类细胞时，可结合该类细胞的绝对计数、比例和白细胞总数。"),
    Note("lymphocytes", ("LYM#", "LYM%"), "NHS · 淋巴细胞计数降低",
         "https://rms.cornwall.nhs.uk/primary_care_clinical_referral_criteria/rms/primary_care_clinical_referral_criteria/haematology/lymphopenia",
         "淋巴细胞计数偏低是非特异性结果。解读需要结合年龄、症状、近期感染、用药和既往血常规。"),
    Note("red_cells", ("HGB", "HCT", "RBC", "MCV"), "MedlinePlus · 全血细胞计数", _MEDLINE + "complete-blood-count-cbc/",
         "血红蛋白参与运输氧气，红细胞计数反映细胞数量，红细胞压积反映红细胞所占体积，平均红细胞体积反映细胞大小。"
         "结合这些结果可以看出变化出现在数量、大小还是血红蛋白浓度上。"),
    Note("platelets", ("PLT",), "MedlinePlus · 全血细胞计数", _MEDLINE + "complete-blood-count-cbc/",
         "血小板参与止血。血小板计数反映血小板的数量。"),
    Note("platelet_size", ("MPV", "PDW", "PCT"), "血液学研究 · 血小板体积指标",
         "https://pmc.ncbi.nlm.nih.gov/articles/PMC6517618/",
         "MPV 是血小板的平均大小，PDW 反映大小的差异，血小板压积反映血小板占全血的体积比例。"
         "血小板压积与血小板计数及平均体积有关，解读时应把这些结果放在一起看。"),
    Note("liver_enzymes", ("ALT", "AST", "LIVER"), "MedlinePlus · 肝功能检查", _MEDLINE + "liver-function-tests/",
         "ALT 和 AST 都是转氨酶。ALT 主要与肝脏相关，AST 也存在于其他组织。结合胆红素等实际报告结果一起看。"
         "有些药物会影响肝功能检查。仅凭一次报告和近期用药史无法判断某项异常是不是药物引起的。"),
)


def _tokens(text: str) -> list[str]:
    return text.split()


@lru_cache(maxsize=16)
def _sparse(ids: tuple[str, ...]) -> BM25Retriever:
    docs = [Document(page_content=f"{' '.join(note.keys)} {note.text}", metadata={"id": note.id})
            for note in _NOTES if note.id in ids]
    return BM25Retriever.from_documents(docs, k=len(docs), preprocess_func=_tokens)


_dense_lock = threading.Lock()


@lru_cache(maxsize=1)
def _local_vectors() -> InMemoryVectorStore | None:
    if os.getenv("EMBEDDING_PROVIDER", "huggingface").lower() != "huggingface":
        return None
    model = os.getenv("EMBEDDING_MODEL") or "BAAI/bge-small-zh-v1.5"
    if not _hf_model_is_cached(model):
        return None
    store = InMemoryVectorStore(get_embeddings())
    store.add_documents([Document(page_content=f"{' '.join(note.keys)} {note.text}", metadata={"id": note.id})
                         for note in _NOTES])
    return store


def retrieve_notes(keys: tuple[str, ...], *, use_vectors: bool = False) -> list[Note]:
    eligible = {note.id: note for note in _NOTES if set(keys).intersection(note.keys)}
    if not eligible:
        return [_LIMIT]
    sparse = _sparse(tuple(sorted(eligible)))
    retriever = sparse
    # The query contains only standardized indicator names, never a report or a question.
    if use_vectors and _dense_lock.acquire(blocking=False):
        try:
            store = _local_vectors()
        finally:
            _dense_lock.release()
        if store is not None:
            dense = store.as_retriever(search_kwargs={"k": len(eligible), "filter": lambda doc: doc.metadata["id"] in eligible})
            retriever = EnsembleRetriever(retrievers=[sparse, dense], weights=[0.6, 0.4], id_key="id")
    documents = retriever.invoke(" ".join(keys))
    return [eligible[doc.metadata["id"]] for doc in documents] + [_LIMIT]


# Keep the percent, count and urine variants distinct when resolving user questions.
_INDICATORS = (
    ("BAS%", r"(?<![A-Z])BAS\s*%|嗜碱(?:性)?粒细胞(?:比率|比例|百分比)"),
    ("BAS#", r"(?<![A-Z])BAS\s*#|嗜碱(?:性)?粒细胞(?:的)?(?:绝对)?(?:计数|数量|数)(?!比)"),
    ("BAS", r"\bBAS(?!\s*[%#]|[A-Z])|嗜碱(?:性)?粒细胞"),
    ("EOS%", r"\bEOS\s*%|嗜酸(?:性)?粒细胞(?:比率|比例|百分比)"),
    ("EOS#", r"\bEOS\s*#|嗜酸(?:性)?粒细胞(?:绝对)?(?:计数|数量)"),
    ("NEUT%", r"\bNEU(?:T)?\s*%|中性粒细胞(?:比率|比例|百分比)"),
    ("NEUT#", r"\bNEU(?:T)?\s*#|中性粒细胞(?:绝对)?(?:计数|数量)"),
    ("LYM%", r"\bLYM(?:PH)?\s*%|淋巴细胞(?:比率|比例|百分比)"),
    ("LYM#", r"\b[LI1]YM(?:PH)?\s*#|淋巴细胞(?:绝对)?(?:计数|数量)"),
    ("MONO%", r"\bMONO?\s*%|单核细胞(?:比率|比例|百分比)"),
    ("MONO#", r"\bMONO?\s*#|单核细胞(?:绝对)?(?:计数|数量)"),
    ("URBC", r"URBC|尿(?:液)?红细胞"),
    ("UWBC", r"UWBC|尿(?:液)?白细胞"),
    ("URINE_BLOOD", r"尿(?:液)?(?:潜血|隐血)|\b(?:NH-BLD|U-BLD)\b"),
    ("URINE", r"尿(?:液)?(?:蛋白|葡萄糖|糖|比重|酸碱度|亚硝酸盐|白细胞酯酶)|\b(?:SG|NIT|LEU|NH-PH)\b"),
    ("HGB", r"\b(?:HGB|HB)\b|(?<!糖化)血红蛋白(?![A-Z])"),
    ("HCT", r"\bHCT\b|红细胞压积|红细胞比容"),
    ("MCV", r"\bMCV\b|平均红细胞体积"),
    ("RBC", r"\bRBC\b|(?<!尿)(?<!尿液)(?<!平均)红细胞(?!压积|比容|体积|分布)"),
    ("WBC", r"\bWBC\b|(?<!尿)(?<!尿液)白细胞(?:总数|计数|(?!(?:分类|比例|百分比)))"),
    ("MPV", r"\bMPV\b|平均血小板体积"),
    ("PCT", r"血小板(?:压积|积压)|\b(?:plateletcrit|thrombocrit|PCT)\b"),
    ("PLT", r"\bPLT\b|血小板(?!压积|分布|体积)"),
    ("ALT", r"\bALT\b|丙氨酸(?:氨基)?转移酶|谷丙转氨酶"),
    ("AST", r"\bAST\b|天门冬氨酸(?:氨基)?转移酶|谷草转氨酶"),
    ("LIVER", r"胆红素|碱性磷酸酶|谷氨酰转移酶|\b(?:TBIL|DBIL|IDBIL|ALP|GGT)\b"),
    ("PDW", r"\bPDW\b|血小板分布宽度"),
    ("CREA", r"\b(?:CREA|SCR)\b|肌酐"),
    ("KIDNEY", r"估算肾小球滤过率|尿素(?:氮)?|\b(?:EGFR|BUN|UREA)\b"),
    ("GLU", r"\bGLU\b|血糖|葡萄糖"),
    ("HBA1C", r"\bHBA1C\b|糖化血红蛋白"),
    ("TG", r"甘油三酯|\b(?:TG|TRIG)\b"),
)


def indicator_keys(text: str) -> tuple[str, ...]:
    matches: set[str] = set()
    occupied: set[int] = set()
    for key, pattern in _INDICATORS:
        for hit in re.finditer(pattern, text, re.I | re.ASCII):
            positions = set(range(*hit.span()))
            if not positions.intersection(occupied):
                matches.add(key)
                occupied.update(positions)
    return tuple(key for key, _ in _INDICATORS if key in matches)


def finding_keys(name: str, unit: str) -> tuple[str, ...]:
    label = normalize("NFKC", name)
    keys = indicator_keys(label)
    if "PCT" in keys and not re.search(r"血小板|plateletcrit|thrombocrit", label, re.I):
        if label.strip().upper() != "PCT" or normalize("NFKC", unit).strip() != "%":
            keys = tuple(key for key in keys if key != "PCT")
    if {"URBC", "UWBC", "URINE", "URINE_BLOOD"}.intersection(keys):
        return tuple(key for key in keys if key not in {"RBC", "WBC", "GLU", "CREA"})
    return keys


def related_keys(key: str) -> tuple[str, ...]:
    if key == "ALT":
        return ("AST",)
    if key == "AST":
        return ("ALT",)
    if key in {"MPV", "PDW", "PCT", "PLT"}:
        return tuple(item for item in ("PLT", "MPV", "PCT", "PDW") if item != key)
    if key in {"HGB", "HCT", "RBC", "MCV"}:
        return tuple(item for item in ("HGB", "HCT", "RBC", "MCV") if item != key)
    if key.endswith(("%", "#")):
        counterpart = key[:-1] + ("#" if key.endswith("%") else "%")
        return counterpart, "WBC"
    return ("BAS%", "BAS#", "WBC") if key == "BAS" else ()
