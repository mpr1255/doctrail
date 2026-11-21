# doctrail native ingestor

This crate is doctrail's document-extraction engine (`doctrail._ingest_native`),
compiled by maturin and shipped inside the `doctrail` wheel under AGPL-3.0-or-later.

Rebuild it in a source checkout with:

```bash
make native
```

Optional runtime safety limits:

- `DOCTRAIL_INGEST_TIMEOUT_MS` (default `20000`)
- `DOCTRAIL_INGEST_MAX_NESTING_DEPTH` (default `8192`)
- `DOCTRAIL_INGEST_MAX_CONTENT_CHARS` (default `2000000`)

The Rust engine parses text, HTML, MHTML, PDF, DOCX, DOC, XLSX, XLS, PPTX,
EPUB, CSV, TSV, and ZIP archives in process. Bounded external lanes handle
MOBI/AZW (`ebook-convert`), DJVU (`djvutxt`), RTF (`textutil`/`unrtf`), and
legacy PPT (`strings`). PDF text comes from `pdftotext` by default with bundled
MuPDF as fallback and `--pdf-engine pymupdf` override. Files that need OCR are
flagged and routed through Doctrail's Python OCR layer (`--ocr-engine`).
