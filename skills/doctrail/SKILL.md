---
name: doctrail
description: "Initialize and operate Doctrail projects: ingest document corpora into SQLite, define SQL-scoped YAML enrichments, run sync or provider-batch LLM coding, inspect normalized audit/enrichment storage through run/pivot/spec views, compare model coders, and finalize human-review datasets."
allowed-tools: Bash
---

# Doctrail

Use this when the user wants to start or operate a Doctrail project: ingest files into SQLite, create or edit enrichment YAML, run LLM coding over SQL-selected rows, inspect run history, build review or analysis views, compare model coders, or repair normalized storage.

This skill is the operating doctrine: the mental model, the judgment rules, and worked examples. The complete reference — every YAML key, every command and flag, the storage contract — ships inside the package: `doctrail docs` prints the whole manual to stdout, offline. When you need a detail not covered here, read the manual rather than guessing.

## Core mental model

Every answer Doctrail has ever produced lives in one long table, `_enrichments`: one row per document, enrichment, field, model, and prompt version. Three kinds of object share the database, distinguishable at a glance:

- Source tables or views keep the original inputs. `documents` is only a default scaffold name.
- Tables prefixed `_` are Doctrail's ledger: `_enrichments` (parsed answers, long form), `_enrichment_audit` (every raw model call and response, plus the exact projection payload used to populate `_enrichments`), `_enrichment_runs` and `_enrichment_run_items` (provenance and the exact input rowset).
- Views prefixed `v_` are what people actually read: `v_run_*` shows one specific run in wide form, `v_final_*` applies human overrides to a run, and `doctrail view pivot` builds reusable wide views over `_enrichments`.

Rule: store long, inspect wide.

## Happy path

1. Inspect the database or current project.
2. Prefer `doctrail init` if starting fresh.
3. Use `doctrail new` or patch `.doctrail/enrichments/*.yml` to define the enrichment.
4. Ingest if needed.
5. Run `doctrail enrich <name> --dry-run`.
6. Run `doctrail enrich <name> --limit 5`.
7. Inspect with `doctrail runs`, `doctrail view create --run-id <run_id>`, `doctrail view spec`, or `doctrail view pivot`.
8. Iterate with `doctrail edit <name>` or direct YAML edits until the prompt and schema are stable.
9. Run the full corpus, using `--execution-mode batch` when direct provider batch mode is appropriate.
10. Materialize the review surface or final editable table with `doctrail view create`, `doctrail view spec`, `doctrail view pivot`, or `doctrail finalize`.
11. Export and import overrides or run ICR if needed.

`doctrail run` is an alias of `doctrail enrich`; the two are interchangeable everywhere.

## Project setup

Prefer using the same CLI that a human user would use.

```bash
doctrail init
doctrail new
doctrail ingest --input-dir docs/ --yes
doctrail enrich language --dry-run
doctrail enrich language --limit 5
```

If `doctrail init` already created `.doctrail/config.yml` and `.doctrail/enrichments/*.yml`, patch those files rather than inventing parallel config formats.

## Offline runs and demos (replay)

The model name `replay` (or `replay/<label>`) returns canned responses from `.doctrail/replay/<enrichment>.jsonl` fixtures instead of calling an API. Everything downstream — audit rows, `_enrichments`, views, runs, ICR — is the real pipeline, so replay runs are how you exercise a config end to end without an API key. Distinct labels act as distinct coders for ICR (`-m replay/coder-a -m replay/coder-b`).

`doctrail init test` scaffolds a complete canned tutorial workspace into the current directory: small public corpora, a pre-ingested `out/database.db`, prepared enrichment configs, and replay fixtures. Use it to walk a new user through the whole loop offline.

## Before running an enrichment

- Inspect the source schema first:

```bash
sqlite3 /path/to/db.db ".tables"
sqlite3 /path/to/db.db ".schema your_source_table"
sqlite3 /path/to/db.db "SELECT * FROM your_source_table LIMIT 3"
```

- Confirm the key column. It must be present in the input query.
- Do not assume the source table is named `documents`. The real contract is: valid SQL input query + stable key column + schema.
- Keep prompts and schema small on the first pass.
- Use `--output-db` when the user wants a non-destructive run into a separate database.
- For targeted reruns, prefer `--where "..."` to filter the enrichment's existing query; use `--query` only when replacing the SQL entirely.
- Default to `--dedupe-scope query` unless the user explicitly wants prompt-family reuse.
- Append-mode dedupe is success-based: a row is only "already done" when a successful normalized result exists in `_enrichments` for the current dedupe scope.
- Null answers count as answered. A field the model explicitly returned as null is stored as a row with a NULL value, and dedupe treats that row as complete; rerunning without `--overwrite` will not re-ask it.
- Raw attempt history in `_enrichment_audit` and per-run snapshots in `_enrichment_run_items` are provenance, not completion signals. Error-only rows should retry without `--overwrite`.
- Use `--overwrite` to force reruns of rows that already succeeded.

## Enrichment config rules

- `input.query` may be a named SQL query or inline SQL.
- `input.input_columns` controls what the model sees.
- Use `table.column` for multi-table inputs.
- Use `:N` suffix to truncate large fields, for example `raw_content:3000`.
- `output_column` can still name a single extracted field.
- For short screening tasks, use `pack_size` to group multiple rows into one structured call.
- `pack_response_mode: selected_indexes` is the compact mode for a single boolean field. The model returns zero-based matching item indexes only, and Doctrail unpacks them into ordinary row-level `_enrichment_audit` and `_enrichments` rows.
- `pack_response_mode: exhaustive` returns one structured result per packed item. Prefer it for multi-field schemas or when omission risk matters more than output-token savings.
- Packed prompt shaping is separate from `--execution-mode batch`. Today `pack_size` is sync-only.
- Packed mode is most appropriate for short inputs like titles, abstracts, or snippets. It is a poor default for full-document prompts.
- Design schemas so the common answer is cheap. For rare-hit screening over a large corpus, prefer a `false`/`0`/small-enum default over nullable prose fields, and combine it with `pack_size` so non-hits cost almost no output tokens.
- Older configs may include `output_table`, but the core storage model is `_enrichments` plus derived views. Do not describe `output_table` as the primary storage contract.
- Do not name enrichment output fields the same as source table columns. If a schema field matches a source column, Doctrail will error. Rename the field (e.g., `fulltext_cleaned` instead of `fulltext_clean`) or pass `--allow-column-collision` to proceed (the source column becomes `<name>_input` in views).

## Prompt construction

- Write the prompt as a codebook, the way you would brief a research assistant: define every enum value, anchor every scale point (`0 = no mention; 5 = explicit existential threat`), and state explicitly when gated fields must be null. Vague constructs produce coder disagreement, with models exactly as with humans; if ICR comes back low, fix the codebook before blaming the model.
- Provider prompt caching is prefix-based. OpenAI caches eligible matching prompt prefixes automatically. Gemini implicit caching is automatic on Gemini 2.5 and newer models, but Google does not guarantee savings on every request. In both cases, cache reuse depends on the longest identical prefix counted from the top of the request.
- Therefore the rule is: everything static first, everything per-row last. Doctrail's default renderer puts the prompt first, appends schema instructions there for JSON-mode paths or sends provider-native schema payloads as constant request structure, then appends the per-row `input_columns` content at the end. For provider-reported OpenAI or Gemini cache hits, a long codebook prefix can be billed at cached-input rates. Doctrail does not currently set Anthropic `cache_control` markers, so do not count on Anthropic prompt-cache discounts from this layout.
- `{column}` placeholders inside the prompt can break this. Each substitution makes the request differ from row to row at that point, reducing cache reuse for everything after it. Feed per-row content through `input_columns` instead; if you genuinely must interpolate, put the placeholder at the end of the prompt so the static prefix above it still matches.
- Control input size with `:N` truncation on `input_columns` (e.g. `raw_content:3000`) rather than editing source data.

Example packed boolean screen:

```yaml
enrichments:
  - name: packed_relevance
    input:
      query: all_docs
      input_columns: [raw_content:240]
    prompt: |
      Return the items that mention sanctions or trade restrictions.
    output_column: is_relevant
    schema: {type: "boolean"}
    pack_size: 10
    pack_response_mode: selected_indexes
```

## Iterative workflow

Use this workflow for prompt development:

```bash
doctrail enrich classify --dry-run
doctrail enrich classify --limit 10
doctrail runs
doctrail view create --run-id <run_id>
```

Then revise the YAML and rerun on another small sample. Once the prompt is stable:

```bash
doctrail enrich classify
doctrail view create --run-id <final_run_id>
```

If the user wants a reusable wide analysis surface instead of a single-run snapshot:

```bash
doctrail view pivot review_surface -e classify --include "title,raw_content:500"
```

## Provider batch workflow

Use `--execution-mode batch` for direct provider batch runs. The older `--execution-mode openai-batch` spelling still works as a compatibility alias, but do not teach it as the current command.

- Direct OpenAI model ids must appear in Doctrail's verified OpenAI batch catalog.
- That catalog is fetched from the official OpenAI model docs, not guessed from a static local allowlist.
- A model is only accepted for this path if the docs page shows both `v1/batch` and `v1/chat/completions`.
- The catalog stores batch input, cached-input, and output prices per 1M tokens, documented snapshots, and the fetch date.
- Direct OpenAI batch cost estimation uses that catalog, not the normal sync pricing table.

Inspect or refresh the catalog with:

```bash
doctrail models --openai-batch
doctrail models --openai-batch --refresh
```

Operational rule:
- if a direct OpenAI model is outside the verified batch catalog, Doctrail should refuse the batch run instead of silently falling back
- if the docs are unreachable, Doctrail may use the cached or bootstrap catalog, and the CLI should make that visible

Direct batch backends behind `--execution-mode batch`:
- direct OpenAI bare model ids use OpenAI's `/v1/batches` API with request lines targeting `/v1/chat/completions`
- direct Anthropic model ids like `claude-*` or `anthropic/*` use Anthropic's `/v1/messages/batches` API with request params targeting `/v1/messages`
- direct Gemini model ids like `gemini-*` or `models/gemini-*` use Gemini's File API plus `v1beta/models/{model}:batchGenerateContent`
- the verified OpenAI batch catalog only governs the direct OpenAI branch; Anthropic and Gemini do not use that catalog
- submit with `doctrail enrich <name> --execution-mode batch`
- reconcile later with `doctrail batch poll --run-id <run_id>` or `doctrail batch watch --run-id <run_id>`
- after reconciliation, successful rows land in both `_enrichment_audit` and `_enrichments`
- failed rows may still land in `_enrichment_audit`, but remain eligible for retry on the next append-mode run

Monitoring a live batch run — `doctrail batch watch --run-id <run_id>` for a live feed, or query the shard table directly for a precise picture:

```bash
sqlite3 /path/to/db.db "
  SELECT id, status, request_count, completed_count, failed_count,
         output_file_id IS NOT NULL AS has_output,
         error_file_id IS NOT NULL AS has_errors,
         last_polled_at, completed_at, reconciled_at
  FROM _enrichment_batch_jobs
  WHERE run_id = 'your_run_id'
  ORDER BY id
"
```

Large runs are sharded into multiple provider jobs — one row per shard. A run is fully done when every shard has `reconciled_at` set; until then `completed_count` climbs toward `request_count` per shard, and rows land in `_enrichments` only at reconciliation (`batch poll`/`batch watch`), not while the provider is still processing.

Practical recovery rule for partial batch failures:

```bash
doctrail batch poll --run-id <run_id>
doctrail enrich <name>
```

Use `--overwrite` only if you want to rerun rows that already succeeded.

GPT-5 operational note:
- direct OpenAI `gpt-5*` chat-completions runs default to `reasoning_effort=minimal` unless the enrichment config overrides it
- allowed values are `minimal`, `low`, `medium`, `high`
- use that knob deliberately for cost control on both sync and batch runs

## Views

After a run, Doctrail also maintains an automatic wide view over the source table (e.g. `v_documents_enriched`): one row per document, one column per field, latest value wins. For anything beyond that default, use the right view type for the task.

- `doctrail view create --run-id <run_id>`
  - exact snapshot of one run (`v_run_*`)
  - best for pilot runs, final runs, and human review
- `doctrail view create <enrichment>`
  - latest run for that enrichment
  - convenient but less explicit than `--run-id`
- `doctrail view pivot <name> -e <enrichment>`
  - reusable wide view over normalized enrichment data
  - good for coding surfaces and ICR comparison
- `doctrail view spec <name>`
  - YAML-driven review surface for a common workflow
  - supports source columns, extra enrichment columns, and one exploded JSON-array field
- `doctrail view pivot ... --by-model`
  - model-by-model columns for comparison work

For human-readable exports of a materialized view:

```bash
doctrail view render payments_review --output payments_review.html
```

## Review and overrides

Human correction workflow:

```bash
doctrail overrides-export --run-id <run_id>
doctrail overrides-import --run-id <run_id> --input overrides.csv --reviewer alice
```

`v_final_*` views layer overrides on top of the original run output. The storage tables remain unchanged; the view is the merged surface.

Short rule:
- model runs are immutable coder outputs
- compare runs in views, but edit final values in one clean final surface
- agreement between models is ICR
- do not invent duplicate `machine_*` columns in the final editable table
- keep only enough provenance to join back to machine history

Current implementation note:
- `doctrail finalize --run-id <run_id> --table <table_name>` materializes one writable final table from a chosen run
- `doctrail finalize --view <view_name> --table <table_name>` materializes any existing review view into a writable table
- `v_final_*` views plus override import/export still exist for lightweight review and CSV workflows

For repeated extracted items, prefer exploded review views so one extracted item becomes one row.

If the user wants to repair or verify normalized storage from audit:

```bash
doctrail rebuild-enrichments --db-path /path/to/db.db --yes
```

This only works for audit rows that have the persisted projection payload. It is strict by design and will fail rather than guess from legacy raw JSON.

## ICR workflow

For model agreement work:

```bash
doctrail icr classify -m gpt-4o-mini -m openrouter/google/gemini-2.5-flash --sample 100 --seed 42
doctrail icr-report --db-path /path/to/db.db --field category
```

The default install computes Krippendorff's alpha and Cohen's kappa; `doctrail[icr]` remains accepted for older setup scripts but is no longer required.

## Working with the database directly

Everything Doctrail produces lands in plain SQLite, so most inspection and reshaping is just SQL. `sqlite3` covers ad-hoc queries; for anything heavier, `sqlite-utils` is the right companion CLI (reference: https://sqlite-utils.datasette.io/). It is not bundled with doctrail — install it once with `uv tool install sqlite-utils`. A ready reckoner of the moves you will actually make:

- list tables and views: `sqlite-utils tables out/database.db` (add `--views` for views)
- inspect schema: `sqlite-utils schema out/database.db documents`
- peek at a wide view: `sqlite-utils rows out/database.db v_documents_enriched --limit 5`
- run a query as JSON lines: `sqlite-utils out/database.db "SELECT ... FROM v_..." --nl`
- count answered rows for an enrichment: `sqlite-utils out/database.db "SELECT COUNT(DISTINCT key_value) FROM _enrichments WHERE enrichment_name='x'"`
- export a review view to CSV: `sqlite-utils rows out/database.db v_run_<id> --csv > review.csv`

Read-only inspection is always safe. Reshape into your own tables and views freely, but do not hand-edit the `_`-prefixed ledger tables — let doctrail write those, and use overrides/finalize for human corrections.

## Troubleshooting

- `No config found`: run `doctrail init` or pass `--config`.
- `key_column not found`: include it in the SQL `SELECT`.
- Tables named `enrichments` / `enrichment_audit` without the `_` prefix mean a pre-rename database. Any doctrail command migrates it in place automatically (tracked via `PRAGMA user_version`); do not rename tables by hand.
- Unsure what changed between prompt versions: use `doctrail runs` and `doctrail diff-runs`.
- Unsure what the user should inspect: materialize a `v_run_*` view first; it is the safest default review surface.
- `Rows were skipped but should retry`: check `_enrichments`, not just `_enrichment_audit` or `_enrichment_run_items`.

```bash
sqlite3 /path/to/db.db "
  SELECT COUNT(DISTINCT key_value)
  FROM _enrichments
  WHERE enrichment_name = 'your_enrichment'
"
```

- `Batch run shows many errors`: inspect per-row status in the run snapshot.

```bash
sqlite3 /path/to/db.db "
  SELECT status, COUNT(*)
  FROM _enrichment_run_items
  WHERE run_id = 'your_run_id'
  GROUP BY status
"
```

- `Field name collides with source column`: rename the enrichment field if possible. If you need to proceed immediately, use:

```bash
doctrail enrich <name> --allow-column-collision
```
