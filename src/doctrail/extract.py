"""Extract one file's text with the native engine, with no database.

    from doctrail.extract import extract_file

    doc = extract_file("report.pdf")
    if doc["ocr_needed"]:
        ...  # send the file to OCR; doc["ocr_reason"] says why
    for number, text in doc["pages"]:
        ...

The result is the native engine's record for the file (see
`doctrail.ingest.native_extractor`) plus `pages`, a list of
`(page number, text)` for each PDF page that has text. Joined in order, the
page texts make up `content`; a paragraph that runs over a page break is split
between its two pages. Other formats have no pages and return an empty list.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Union


def extract_file(path: Union[str, "os.PathLike[str]"]) -> Dict[str, Any]:
    try:
        from doctrail import _ingest_native as native
    except ImportError:
        try:
            import _ingest_native as native
        except ImportError:
            raise RuntimeError(
                "extract_file needs the native engine, which this install lacks; "
                "build it from a source checkout with `make native`"
            ) from None

    doc = json.loads(native.extract_path(os.fspath(path)))
    content = doc.get("content") or ""
    metadata = (doc.get("extraction_metadata") or {}).get("content_extraction") or {}
    offsets = metadata.get("page_offsets") or []
    ends = [start for _, start in offsets[1:]] + [len(content)]
    doc["pages"] = [
        (page, content[start:end].strip()) for (page, start), end in zip(offsets, ends)
    ]
    return doc
