# doctrail native ingestor

This crate is doctrail's vendored native document-extraction core. Python calls
it through the `doctrail._ingest_native` PyO3 module.

Build it from the repository root with:

```bash
bash scripts/build_native.sh
```

Alternatively, run `make native` from the repository root.

The optional local safety limits are:

- `DOCTRAIL_INGEST_TIMEOUT_MS` (default `20000`)
- `DOCTRAIL_INGEST_MAX_NESTING_DEPTH` (default `8192`)
- `DOCTRAIL_INGEST_MAX_CONTENT_CHARS` (default `2000000`)
