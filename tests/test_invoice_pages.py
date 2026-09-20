"""多页发票金额归并回归，不调用外部 OCR/LLM。"""
import pytest

from document_comparison.statement.invoice_pages import consolidate_invoice_pages


def invoice(number="12345678901234567890", total="1,130.00"):
    return f"电子发票\n发票号码：{number}\n价税合计（大写）壹仟壹佰叁拾元整（小写）￥{total}"


def consolidate(texts):
    return consolidate_invoice_pages(dict(enumerate(texts)), file_index=0, file_name="test.pdf")


def test_same_invoice_total_counted_once():
    summaries, pages, diagnostics = consolidate([invoice(), invoice(), invoice()])
    assert pages == {0, 1, 2}
    assert not diagnostics
    assert len(summaries) == 1
    assert summaries[0].tax_inclusive_total == 1130
    assert summaries[0].items[0].canonical == "CNY:1130.00"
    assert "1, 2, 3" in summaries[0].tax_inclusive_method


def test_distinct_invoices_with_equal_amount_are_not_deduplicated():
    summaries, pages, diagnostics = consolidate([
        invoice(), invoice(), invoice(number="12345678901234567891"),
        invoice(number="12345678901234567891"),
    ])
    assert pages == {0, 1, 2, 3}
    assert not diagnostics
    assert [s.tax_inclusive_total for s in summaries] == [1130, 1130]


@pytest.mark.parametrize("second", [invoice(total="2260.00"), "电子发票\n发票号码：12345678901234567890"])
def test_conflicting_or_partially_missing_total(second):
    summaries, pages, diagnostics = consolidate([invoice(), second])
    assert pages == {0, 1}
    if "2260" in second:
        assert not summaries
        assert all(not d.reliable for d in diagnostics)
    else:
        # 同票号续页不印价税合计时仍只用该票唯一明确合计。
        assert summaries[0].tax_inclusive_total == 1130
        assert not diagnostics


def test_missing_numbers_are_not_silently_deduplicated_by_amount():
    summaries, pages, diagnostics = consolidate([invoice(number=""), invoice(number="")])
    assert not summaries
    assert pages == {0, 1}
    assert len(diagnostics) == 2
    assert "票号" in diagnostics[0].reasons[0]


def test_subtotals_are_not_used_as_invoice_grand_total():
    text = "电子发票\n发票号码：12345678901234567890\n合计1000元\n税额130元"
    summaries, pages, diagnostics = consolidate([text, text])
    assert not summaries
    assert pages == {0, 1}
    assert diagnostics


def test_single_invoice_and_statements_keep_original_extraction():
    assert consolidate([invoice()]) == ([], set(), [])
    assert consolidate(["对帐单\n合计1130元", "对帐单\n合计1130元"]) == ([], set(), [])


def test_legacy_invoice_codes_distinguish_equal_numbers():
    texts = [
        invoice(number="12345678") + f"\n发票代码：{code}"
        for code in ["123456789012", "123456789012", "123456789013", "123456789013"]
    ]
    summaries, _, diagnostics = consolidate(texts)
    assert len(summaries) == 2
    assert not diagnostics


def test_html_and_split_labels_are_supported():
    text = "<table><tr><td>电子发票</td><td>发票 号码：</td><td>12345678901234567890</td></tr>" \
           "<tr><td>价 税 合 计</td><td>（小写）</td><td>￥１，１３０．００</td></tr></table>"
    summaries, _, diagnostics = consolidate([text, text])
    assert summaries[0].tax_inclusive_total == 1130
    assert not diagnostics


def test_statement_invoice_references_do_not_trigger_invoice_deduplication():
    text = "对帐单\n发票号码：12345678901234567890\n含税金额1130元"
    assert consolidate([text, text]) == ([], set(), [])


def test_multiple_invoice_numbers_on_one_page_require_review():
    text = invoice() + "\n" + invoice(number="12345678901234567891")
    summaries, pages, diagnostics = consolidate([text, text])
    assert not summaries
    assert pages == {0, 1}
    assert diagnostics
