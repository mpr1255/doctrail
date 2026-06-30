import pytest

from doctrail.ingest.text_processing import clean_ocr_text


def test_clean_ocr_text_preserves_linebreaks():
    raw = "Line one\nLine two\n\nLine three"
    cleaned = clean_ocr_text(raw)

    # Ensure core content survives and line breaks separate sentences.
    assert "Line one" in cleaned
    assert "Line two" in cleaned
    assert cleaned.count("\n") >= 2


def test_clean_ocr_text_normalizes_whitespace_without_collapsing_lines():
    raw = "Line one\r\n   Line   two\f\nLine   three"
    cleaned = clean_ocr_text(raw)

    # Form feed becomes a blank line, and internal spacing is normalized
    assert cleaned.splitlines() == [
        "Line one",
        "Line two",
        "",
        "Line three",
    ]
