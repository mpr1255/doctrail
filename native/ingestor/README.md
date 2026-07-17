# doctrail native ingestor

This crate is doctrail's vendored native document-extraction core. Python calls
it through the `doctrail._ingest_native` PyO3 module.

Build it from the repository root with:

```bash
bash scripts/build_native.sh
```

Alternatively, run `make native` from the repository root.
