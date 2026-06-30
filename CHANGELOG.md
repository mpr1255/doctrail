# Changelog

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

- Doctrail now prefers the nearest project-local `.env` over inherited shell or global environment variables.
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
