"""同一 PDF 内的多页发票归并。仅凭明确票号归并，不按金额相等去重。"""
from __future__ import annotations

import html
import re
import unicodedata
from decimal import Decimal

from ..models import PageRecognitionDiagnostic, StatementAmountItem, StatementTableSummary


def _normalize(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _invoice_key(text: str) -> tuple[str, str] | None:
    numbers = set(re.findall(r"发票(?:号码|号)[:：|]*([0-9]{8,20})(?![0-9])", text))
    if len(numbers) != 1:
        return None
    codes = set(re.findall(r"发票代码[:：|]*([0-9]{10,12})(?![0-9])", text))
    if len(codes) > 1:
        return None
    return (next(iter(codes), ""), next(iter(numbers)))


def _totals(text: str) -> list[tuple[Decimal, str]]:
    # 必须紧跟价税合计字段，或其大写金额后的“小写”字段；不取普通合计/小计。
    number = r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?(?![0-9.,%])"
    separator = r"[():：|]*"
    pattern = (
        r"价税合计" + separator
        + r"(?:大写[\u4e00-\u9fff():：|]{0,80})?"
        + r"(?:小写" + separator + r")?[¥￥]?" + separator
        + r"(?P<amount>" + number + r")"
    )
    return [
        (Decimal(m.group("amount").replace(",", "")), m.group(0))
        for m in re.finditer(pattern, text)
    ]


def consolidate_invoice_pages(
    page_texts: dict[int, str], *, file_index: int, file_name: str,
) -> tuple[list[StatementTableSummary], set[int], list[PageRecognitionDiagnostic]]:
    """重复票号的多页发票使用一次明确价税合计；冲突或归属不明时暂不计入。

    返回已归并的金额、应从逐页抽取中排除的页、复核诊断。单页发票保持原路径。
    """
    normalized = {p: _normalize(t) for p, t in page_texts.items()}
    invoice_pages = {
        p: t for p, t in normalized.items()
        if ("发票" in t and "价税合计" in t)
        or (_invoice_key(t) is not None and ("电子发票" in t or "增值税" in t))
    }
    keys = {_invoice_key(t) for t in invoice_pages.values()} - {None}
    invoice_pages.update({p: t for p, t in normalized.items() if _invoice_key(t) in keys})
    if len(invoice_pages) < 2:
        return [], set(), []

    groups: dict[tuple[str, str], list[int]] = {}
    unknown: list[int] = []
    for page, text in invoice_pages.items():
        key = _invoice_key(text)
        if key is None:
            unknown.append(page)
        else:
            groups.setdefault(key, []).append(page)

    summaries: list[StatementTableSummary] = []
    handled: set[int] = set()
    diagnostics: list[PageRecognitionDiagnostic] = []

    def review(pages: list[int], reason: str) -> None:
        handled.update(pages)
        diagnostics.extend(
            PageRecognitionDiagnostic(
                page_index=p, source="fallback", reliable=False, reasons=[reason],
            ) for p in pages
        )

    if unknown:
        review(unknown, "多页发票缺少明确票号，无法确认是否重复；这些页的金额暂不计入，请人工复核")

    for pages in groups.values():
        if len(pages) < 2:
            continue
        candidates = [(p, value, raw) for p in pages for value, raw in _totals(normalized[p])]
        values = {value for _, value, _ in candidates}
        if len(values) != 1:
            review(pages, "同一票号的多页价税合计缺失或不一致，金额暂不计入，请人工复核")
            continue
        page, amount, raw = candidates[0]
        handled.update(pages)
        value = float(amount)
        column = "价税合计"
        summaries.append(StatementTableSummary(
            file_index=file_index, file_name=file_name, page_index=page,
            headers=[column], column_sums={column: value},
            declared_totals={column: value}, column_source={column: "heuristic"},
            tax_inclusive_total=value,
            tax_inclusive_method="同一票号价税合计仅计一次（页 " + ", ".join(str(p + 1) for p in pages) + "）",
            items=[StatementAmountItem(
                file_index=file_index, file_name=file_name, page_index=page,
                column=column, row_label="发票价税合计", raw_cell=raw,
                canonical=f"CNY:{amount}", value=value,
            )],
        ))
    return summaries, handled, diagnostics
