"""Extract one file's text with Doctrail's extraction engine, with no database.

    from doctrail.extract import extract_file

    doc = extract_file("report.pdf")
    if doc["ocr_needed"]:
        ...  # send the file to OCR; doc["ocr_reason"] says why
    for number, text in doc["pages"]:
        ...

The result is the native engine's record for the file plus ``pages``, a list of
``(page_number, text)`` pairs for each PDF page that has text, with leading and
trailing whitespace trimmed per page. A paragraph that runs over a page break is
split between its two pages. Other formats have no pages and return an empty
list.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Union

from .ingest import native_extractor


def extract_file(
    path: Union[str, "os.PathLike[str]"],
    *,
    html_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Extract text, metadata, OCR signals, and PDF page slices from one file."""
    doc = native_extractor.extract_one(path, html=html_config)
    content = doc.get("content") or ""
    metadata = (doc.get("extraction_metadata") or {}).get("content_extraction") or {}
    offsets = metadata.get("page_offsets") or []
    ends = [start for _, start in offsets[1:]] + [len(content)]
    doc["pages"] = [
        (page, content[start:end].strip()) for (page, start), end in zip(offsets, ends)
    ]
    return doc
