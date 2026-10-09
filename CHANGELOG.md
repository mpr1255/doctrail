# Changelog

## 0.3.7 - text encoding and file-type fixes

The native engine misread some files: text with a few corrupt bytes, and Word, gzip, and image files saved under web-page or `.txt` names. This release reads them correctly and makes repeated ingests into a large database faster.

### Fixes

- Every ingest ran a timestamp backfill that scanned the whole table, which on a 13 GB database took about five minutes per run. The backfill now runs only when it adds the `added_at` column, and re-ingesting 400 files into that database takes about 16 seconds.
- Text and HTML files that are UTF-8 apart from a few stray bytes, as when saved pages were cut or joined mid-character, were read as Windows-1252, because one invalid byte rules UTF-8 out for the encoding detector. The native engine now reads such a file as UTF-8 when valid multi-byte characters outnumber the invalid sequences twenty to one, far more than text in a legacy encoding forms by chance.
- Chinese, Japanese, or Korean text in a legacy encoding with a few corrupt bytes, as in recovered or hard-wrapped files, was read as Windows-1252 for the same reason. The native engine now decodes such text with each CJK encoding that the bad bytes ruled out. It keeps an encoding only if that encoding has the fewest errors, under 2% of the non-ASCII bytes, and the detector picks it once the bad bytes are removed. On 1,990 saved pages and text files in eleven other languages, it changed one file, which was Chinese. Rows ingested before either fix keep their misdecoded text until re-ingested with `--overwrite`.
- The native engine now checks files named `.txt` before trusting the extension, as it already did for web-page names. A gzip-compressed file is decompressed and read, an image goes to OCR, and audio, video, or an archive is recorded as failed. A legacy Word document saved under a web-page or `.txt` name is read with antiword, where its bytes were stored as text.

## 0.3.6 - full-page HTML mode and HTML profiles

Much research data is saved web pages, and this release makes their ingest tunable per collection. The rules live in a YAML profile, and the [HTML profiles](https://doctrail.org/html/) page documents every option; `doctrail ingest --help` links to it.

### Full-page HTML mode

- `--html-mode full` keeps all visible text on HTML and MHTML pages, where the native engine's default article mode keeps only the main article. Full mode renders with html2text: plain text, no link footnotes, table cells one per line, image alt text kept, and no hard line wrapping, so phrase and trigram searches match across the whole paragraph. It runs about six times faster than article mode on a 5,000-page sample.
- `--html-profile FILE` applies per-corpus cruft rules in full mode: `keep_selectors`, `drop_selectors`, `drop_line_patterns`, and `reject_low_value`. Rules are validated before any file is read, and each row records the mode, the profile, and how many elements and lines were removed. `examples/html-profiles/cnki-detail.yml` is a worked profile, and `scripts/html_mode_report.py` lists the lines repeated across a corpus to help write one.
- Full mode keeps login pages, error templates, and navigation-only pages that article mode skips, and records why they looked low-value.
- Profile rules never fall back to an extractor that cannot apply them: a page the native renderer cannot handle is recorded as failed, and a page the rules leave empty is skipped.
- `--html-mode` also selects the Python engine's HTML path: readability for `article`, BeautifulSoup for `full`. Without the flag both engines keep their previous defaults.

### Fixes

- HTML that declares `charset=utf-16` in a `<meta>` tag but is UTF-8, as on many saved CNKI pages, is decoded as UTF-8, as browsers do. Previously the native engine produced mojibake for these pages. The encoding sniffer also no longer rules out UTF-8 when its 10,000-byte sample ends inside a multi-byte character. Rows ingested earlier keep their misdecoded text until re-ingested with `--overwrite`.
- ZIP archives made by Chinese, Japanese, or Korean Windows tools store member names in a legacy encoding without the UTF-8 flag. The native engine now detects the encoding for each archive and decodes these names, which it previously read as CP437 box-drawing characters. It decodes only when the names hold about five or more CJK characters, because the detector guesses shorter samples wrongly; such archives, and archives with Western names, keep CP437 as before. Names that are valid UTF-8 are read as UTF-8. A decoded name that would point outside the archive is rejected.
- The native engine checks the first 64 KiB of a file with a web-page name before trusting its extension. An image saved as `.html` now goes to OCR instead of being stored as decoded bytes. A page saved gzip-compressed is decompressed and read, where it was stored as compressed bytes. Audio, video, or another compressed archive is recorded as failed. A 1.4 GB video saved as `.html` had stalled a worker for over two minutes.
- The low-value checks could discard real text. The login check rejected any page that carried a known login form, including a record saved below one; it now rejects a page only when little prose remains outside the form, not counting links. Bare login pages are still rejected, and a full-mode profile with `reject_low_value` keeps the records. The HTTP error check no longer rejects long pages that discuss an error. The leaked-script check matched prose such as "dysfunction", "function (a)", or a quoted `window.location`; it now looks for code shapes, such as a function with a body or a call or assignment on a member of `document` or `window`.
- MHTML parts without a charset that are valid UTF-8 are read as UTF-8. A rescue for misread GBK had turned French and other accented UTF-8 text into Chinese characters.
- A code book that put `pattern` on a string field failed with a Pydantic error. The pattern now constrains the value.
- The cost estimate counts input columns that the prompt does not name as placeholders, applies `:N` limits, and prices the row nearest the mean length instead of the first row.
- `doctrail init` now defaults to `gemini-2.5-flash` and `claude-haiku-4-5`, replacing older model names.

### Documentation

- `docs/llms.txt` opens with how to install and start, prints each CLI alias as one line instead of repeating its help, and replaces the home page's animations with their descriptions. Detailed reference pages, such as the HTML profiles page, are linked from it rather than included.
- The home page, quick start, code-book, and data-model pages were checked against the code and corrected: what `--overwrite` does to run views, what `icr-report` compares, where Anthropic prompt caching applies, what `init --yes` needs, and which OCR tools doctrail calls.

## 0.3.5 - pdftotext by default, native ingest, OCR routing

### pdftotext is the default PDF extractor

- PDF text now comes from `pdftotext` (Poppler) in both extraction engines, because in the maintainer's use it gives consistently better text than MuPDF. The Python engine tries pdftotext, then pymupdf, then mutool; the native engine runs pdftotext per file and falls back to its bundled MuPDF only when pdftotext fails on that file, recording `pdf_text_fallback_from` in the row metadata.
- Poppler cannot be bundled with the package, so it is now a system requirement for PDF ingest. Doctrail looks for `pdftotext` on `PATH`, then in `/opt/homebrew/bin`, `/usr/local/bin`, and `/usr/bin`; set `DOCTRAIL_PDFTOTEXT` to point at another location. When the batch contains PDFs and the binary cannot be found, ingest stops before extraction with install instructions instead of silently switching engines.
- `--pdf-engine pymupdf` selects MuPDF in both engines. Previously the native engine ignored `--pdf-engine` entirely.
- Native PDF extraction no longer serializes every file behind MuPDF's global lock; only MuPDF fallbacks take it.
- `extraction_method` now reads `pdftotext` (Python engine) or `pdftotext_smart_paragraphs` (native engine) for PDFs. Rows ingested earlier keep their MuPDF-derived text; re-ingest with `--overwrite` to replace it.
- The offline tutorial (`doctrail init test`) still pins MuPDF, so it runs without Poppler.

### Native ingest engine

- Added an optional Rust extraction engine (`--extractor rust`, or `auto` when built with `make native` from a source checkout) that extracts in parallel in-process, with per-file panic containment and bounded batches. It is never shipped in the wheel because it statically links AGPL-licensed MuPDF; `--extractor auto` falls back to the Python engine with a notice when it is absent.
- The native engine covers the same formats as the Python engine, expands ZIP archives with size and entry limits, resumes interrupted ingests, and records per-file failures in the ingest log.
- Ingest adds immutable ingestion timestamps, sanitizes all text at the storage boundary, recovers mislabeled and damaged legacy documents without OCR where text is recoverable, and waits for SQLite writer locks instead of failing under concurrent readers.
- `--skip-embedded-media` ingests Office files as text only, without LibreOffice conversion or embedded-image OCR.
- `--fts-tokenizer trigram` builds a full-text index that can search CJK text; the default `unicode61` tokenizer cannot.

### OCR

- Embedded Office images are OCR'd and merged into their parent document's text.
- A self-hosted OCR service can be used with `--ocr-engine mac-ocr` and `MAC_OCR__SERVICE_ENDPOINTS`, or a custom client via `DOCTRAIL_MAC_OCR_CLIENT_PATH`; the protocol is documented in the quickstart. Uploads are idempotent, retries return to the node holding the cached file, and transient server errors fail over to the next node or to local OCR.
- `DOCTRAIL_OCR_ENGINE=mac-ocr-only` uses only the OCR service and never falls back to local OCR.
- False OCR and legacy-text candidates are rejected before they replace real text.

### Models

- Added self-hosted OpenAI-compatible endpoints, including direct vLLM and Ollama, with provider-native JSON schemas.
- `cli/codex/<model>` accepts any Codex CLI model name and passes `reasoning_effort` through to Codex.

### Fixes

- CI now installs Poppler, and the one native-only test that ran without the native extension now skips like the others.
- Third-party debug logging is filtered at the log handlers.

## 0.3.4 - provider batch schema enforcement

### Batch structured output enforcement

- Fixed direct OpenAI batch requests so structured enrichments send provider-native strict JSON schema in `response_format` instead of JSON mode plus prompt-injected schema text.
- Fixed direct Gemini batch requests so structured enrichments send provider-native `responseSchema` in `generationConfig` instead of prompt-injected schema text.
- Fixed Gemini schema cleanup to inline Pydantic `$defs` / `$ref` definitions before submitting `responseSchema`; Google's `responseSchema` field accepts an OpenAPI-style schema object and rejects `$ref` in live batch JSONL requests.
- Confirmed Anthropic batch already uses native `output_config.format.type = json_schema`; no code change was needed for Anthropic schema enforcement.
- Added regression coverage asserting OpenAI and Gemini batch request bodies carry enum constraints in provider-native schema fields and do not paste schema text into batch prompts.
- Added regression coverage for Gemini Pydantic enum schemas so request bodies contain inline enum values and no `$defs` / `$ref`.

### Live endpoint verification

- OpenAI batch endpoint: patched Doctrail 0.3.4 source completed run `c5f248dfcd0d28259ece2abcfcea60ff061b4c3dfd004d680d9b7701237e470a` against `batch_6a49196822648190877b2e31ccb920ab` with 1 success and 0 errors; the same OpenAI strict-schema path had also completed a 20-row installed 0.3.3 smoke run with 20 successes and 0 Doctrail errors.
- Anthropic batch endpoint: patched Doctrail 0.3.4 source completed run `758ee999c6f4955d5a4fe3489cbc43ce3edb4b74a21ff4449d8bb49e33b54afb` against `msgbatch_01U8CyzqcHu4zHmmwE968VZZ` with 1 success and 0 errors.
- Gemini batch endpoint: installed Doctrail 0.3.3 rejected the live enum-schema smoke request because the submitted schema still contained `$ref`; patched source then completed run `3ce1978f9a3577ff2ee29ee74baf54853daa09543c39cc62aa8993fac92c649a` against `batches/fdvvjqbu7o934ljidw7fy39e66d89itkymw2` with 1 success and 0 errors.

## 0.3.2 - security, rerun, and release polish

### Security and environment safety

- Changed `doctrail serve` to bind `127.0.0.1` by default instead of `0.0.0.0`.
- Disabled legacy write-capable HTTP endpoints by default; `/enrich`, `/ingest`, and `/export` now require explicit `--enable-write-api`.
- Required a bearer token when legacy write endpoints are enabled on a non-loopback server host.
- Changed runtime `.env` discovery so only marked Doctrail project `.env` files are loaded. They override inherited shell keys as the project-local source of truth; bare unrelated cwd `.env` files and package/global `.env` files are ignored.
- Brought Zotero plugin credential loading under the same marked-project `.env` precedence policy.

### Rerun, cost, and billing correctness

- Fixed query-scope append-mode planning so skipped rows are removed before cost estimation, run creation, and provider submission.
- Fixed skipped reruns so they report zero processed rows and no longer print a run-view query for a view that was never created.
- Fixed run-view creation to return no view artifact when a run has no enrichment fields.
- Fixed structured-output persistence failures so a SQLite write error does not trigger a second billed legacy LLM call.
- Added active provider-batch detection for OpenAI, Anthropic, and Gemini so duplicate in-flight batch submissions are blocked before upload/submission.
- Fixed OpenAI structured-output fallback usage accounting so billed failed tiers are included when a later tier succeeds.

### Tutorial, review, export, and server cleanup

- Made quickstart and tutorial both use the full `doctrail init test` fixture.
- Removed `consensus_author` from the tutorial model input while keeping it available as source metadata for comparison.
- Renamed the tutorial repeated-item field to `mentioned_country` so generated views do not collide with the source `country` column.
- Added `--key-column` support to `doctrail review` and fixed the review server join for non-`sha1` projects.
- Sanitized export naming patterns so row-sourced filenames cannot escape the configured output directory.
- Removed stale `doctrail sync` search guidance and made unsupported Chroma server search return a clear unavailable response.
- Corrected server enrichment help so it no longer advertises unimplemented `/db/{name}/enrich` routes.
- Removed dead shipped modules for the old web ingestor, unused config abstraction, and unused LLM client.

### Packaging and docs

- Added PyPI project metadata: authors, classifiers, homepage, documentation, source, issues, and changelog links.
- Moved FastAPI/Uvicorn into the `server` extra while keeping server tests covered through the test extra.
- Added an OpenAI SDK floor and next-major cap.
- Hardened configured table/key/content identifier handling across server, search, query, and ingest read paths.
- Fixed stale doctrail.dev-era/generated-doc references, docs typos, and regenerated `docs/llms.txt`.

## 0.3.1 - revamp release readiness

### Documentation and tutorial fixtures

- Added a terminal demo GIF (ingest, codebook, enrich, query) to the README and docs landing page, rendered reproducibly from `scripts/demo.tape` via `scripts/build_demo_gif.sh`.
- Fixed Office and ebook ingestion so tuple-returning extractors populate document text instead of silently recording empty content, and hardened the tutorial corpus against short extracted files.
- Added committed public-domain extraction fixtures across supported ingest file families and documented the supported local file types in ingest help and quickstart docs.
- Added generated Click-backed CLI documentation, packaged `doctrail docs` manual output, and CI drift checks for CLI, YAML snippets, and `llms-full.txt`.
- Added packaged `doctrail skill` output and `doctrail skill --install` for installing the Doctrail operating doctrine into agent skill directories.
- Added codebook-quality prompt guidance to presets, generated enrichment scaffolds, and YAML docs, with cache-prefix caveats for provider prompt caching.
- Fixed `doctrail new -p` flag mode so scaffold generation stays non-interactive and reports a clear terminal-required error for wizard-only paths.
- Added a fully offline `doctrail init test` tutorial scaffold with replay fixtures, Federalist Papers examples, UN speech excerpts, and the `doctrail run` alias.
- Added tutorial ICR replay examples that bracket agreement quality: a crisp `mentions_climate` boolean codebook and a deliberately under-specified `optimism` score.
- Replaced the tutorial's second enrichment with `securitization`, including replay fixtures that demonstrate gate-dependent null fields.
- Regenerated the UN speech tutorial corpus as deterministic PDF, DOCX, and HTML containers and re-keyed the UN replay fixtures to the new file hashes.
- Renamed the repository tutorial fixture directory from `examples/tutorial/data/` to `examples/tutorial/corpus/` so ignored `data/` directories stay local-only.
- Added source context to model-by-model pivot views so ICR disagreements can be diagnosed directly from the generated review surface.

### Storage and provenance

- Renamed the package layout to `src/doctrail` and kept the legacy import surface working through compatibility exports.
- Added normalized enrichment identity: one current row per key, enrichment name, field, model, and prompt hash, enforced by a unique index and upsert semantics.
- Preserved superseded enrichment rows in side tables during identity migration instead of silently losing recoverable values.
- Renamed Doctrail-managed physical tables with a leading underscore and managed views with a `v_` prefix so source tables and review surfaces are easier to distinguish.
- Added prompt, query, run, and project provenance across `_prompts`, `_enrichment_audit`, `_enrichment_runs`, and `_enrichment_run_items`.
- Added ordered SQLite schema migrations stamped with `PRAGMA user_version`, with the existing idempotent schema guards folded into baseline migration 1.
- Recorded parsed null answers as completed normalized rows so append mode does not resubmit already answered rows.

### Execution and review surfaces

- Added query-scoped and enrichment-scoped dedupe paths so append mode can skip successful prior work without treating audit rows alone as completion.
- Expanded run-aware view creation: run views, final views, pivot/spec/render surfaces, and editable final tables now use the persisted run ledger.
- Added ICR and override workflows backed by SQLite tables so modeled output, human overrides, and finalized review surfaces remain separate.
- Silenced cost/pricing warnings for replay-backed tutorial models while preserving warnings for real unknown models.

### Release preparation

- Added a manual-only release workflow that builds and checks artifacts by default; publishing remains inert unless explicitly enabled with a configured PyPI token.
- Refreshed the documented configuration surface, including stable, deprecated-but-working, and internal keys.

## 2026-03-30 - batch backends, rerun selectors, and env precedence

### Batch execution

- `--execution-mode openai-batch` now maps direct providers to their native batch APIs while keeping one doctrail-facing workflow:
  - OpenAI: `/v1/batches` with request lines targeting `/v1/chat/completions`
  - Anthropic: `/v1/messages/batches` with request params targeting `/v1/messages`
  - Gemini: File API upload plus `/v1beta/models/{model}:batchGenerateContent`
- Batch submit, poll, watch, reconcile, and cancel now work through the same CLI path for direct OpenAI, Anthropic, and Gemini models.
- CLI help, README, docs, and the doctrail skill now make the provider-specific endpoint mapping explicit.

### Anthropic batch hardening

- Added direct Anthropic batch support for `claude-*` and `anthropic/*` models behind the existing batch mode.
- Added provider-side schema compatibility handling for Anthropic structured batch output:
  - bounded integer fields no longer emit unsupported `minimum` / `maximum` constraints into the submitted Anthropic batch schema
  - doctrail now warns about those compatibility issues before submission
- Fixed Anthropic batch polling so provider error objects are serialized cleanly instead of causing downstream JSON serialization failures during reconciliation.
- Live smoke verification completed successfully with `claude-haiku-4-5`.

### Gemini batch changes

- Added direct Gemini batch support for `gemini-*` and `models/gemini-*`.
- Initial Gemini support used inline requests; doctrail now defaults to Google's recommended file-backed batch input mode for Gemini jobs.
- Gemini batch JSONL request lines are now emitted in the file-input shape Google expects, with stable per-row `key` values for reconciliation.
- Gemini batch results are now downloaded and reconciled from the provider result file when available.
- Live verification confirmed that the file-backed path is accepted by Google and produces real `files/...` input handles plus real `batches/...` jobs.
- Operational caveat: Gemini Batch remains unreliable in practice. Live testing saw long-lived `BATCH_STATE_PENDING` jobs and later `503 UNAVAILABLE` responses from Google's GET batch endpoint after roughly 24 hours. This appears to be a provider-side reliability issue rather than a doctrail endpoint or model-id mismatch.

### Targeted reruns

- Added `doctrail enrich --where "..."` to filter an enrichment's existing base query with an outer SQL `WHERE` predicate.
- `--query` remains available as the full-query replacement escape hatch.
- This makes targeted reruns like date filters, `LIKE`, and explicit key lists possible without cloning the YAML prompt/schema definition.

### Environment precedence

- Doctrail now prefers the nearest marked project-local `.env` over inherited shell environment variables.
- This applies to provider resolution and cost/model utilities, so a project can reliably use its own keys without depending on the caller's ambient shell state.

## 2026-01-15 - UX overhaul for social scientists

### New commands

- **`doctrail new`** - Create custom enrichments interactively
  - Interactive mode: `doctrail new`
  - Quick mode: `doctrail new topic -p "Classify topic" -o topic --enum "a,b,c"`
  - Supports: string, integer, boolean, array, enum types

- **`doctrail view`** - Manage database views
  - `doctrail view` - list views in database
  - `doctrail view refresh` - execute all `.doctrail/views/*.sql` files
  - `doctrail view new <name>` - create custom view SQL template

- **`doctrail query`** - Query database without needing sqlite-utils
  - `doctrail query` - list documents
  - `doctrail query 1` - show document #1 details
  - `doctrail query "SELECT ..."` - run arbitrary SQL

### Auto-generated views

After enrichment completes, a queryable view is automatically created:
```
📊 View updated: enrichments_doctrail_demo
   Query with: doctrail query "SELECT * FROM enrichments_doctrail_demo LIMIT 10"
```

This pivots the long-format `_enrichments` table into wide format for easy querying.

### Preset enrichments with aliases

- Built-in presets: `summarize`, `language`, `sentiment`, `document_type`, `relevance`, `keywords`, `extract_entities`, `research_methods`
- British/Australian aliases: `summarise` → `summarize`, `lang` → `language`
- Presets auto-copy to project folder when first used (so users can edit them)

### Schema fixes

- Fixed bare type schemas like `{type: string}` being misinterpreted
- Now correctly wraps with `output_column`: `{summary: {type: string}}`
- Also handles `{enum: [...]}` and `{enum_list: [...]}` bare schemas

### Project tagging

- Enrichments now tagged with `project_name` from config by default
- Enables project-based filtering and automatic view creation

### Python-first extraction (from earlier session)

- PDF: pymupdf as primary (260x faster than OCR-first approach)
- EPUB: ebooklib as primary
- DOCX: python-docx
- System tools (pdftotext, mutool, etc.) now fallbacks
