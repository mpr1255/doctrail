# doctrail native ingestor

This crate is doctrail's vendored native document-extraction core. Python calls
it through the `doctrail._ingest_native` PyO3 module.

Build it from the repository root with:

```bash
bash scripts/build_native.sh
```

Alternatively, run `make native` from the repository root.

The native crate retains its original AGPL-3.0 license marker. Keep it on the
local-development path until its distribution terms are reconciled with the
MIT-licensed top-level package.

The optional local safety limits are:

- `DOCTRAIL_INGEST_TIMEOUT_MS` (default `20000`)
- `DOCTRAIL_INGEST_MAX_NESTING_DEPTH` (default `8192`)
- `DOCTRAIL_INGEST_MAX_CONTENT_CHARS` (default `2000000`)

The Rust path directly parses the common text, HTML, PDF, Office, EPUB, and
spreadsheet formats. Its bounded external lanes use `ebook-convert` for
MOBI/AZW, `djvutxt` for DJVU, `textutil` or `unrtf` for RTF, `strings` for
legacy PPT, `textra` or `tesseract` for images, and `ocrmypdf` for scanned
PDFs. Every external process has a hard timeout, and captured output is limited
to 64 MiB.
