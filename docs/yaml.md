# Code books

A code book is the small YAML file you write for each enrichment: it says which rows to read, what to ask, and what shape the answer must take. (YAML is just the plain-text format it is written in — and you will usually have an agent write it.) A code book draws on four ideas: a database, SQL queries, enrichments, and schemas. The tested examples in `tests/schema_examples/` are the first source of truth. The snippets below are either copied from that corpus or smoke-checked with `doctrail enrich --dry-run`.

## Minimal single-field enum

Source: `tests/schema_examples/single_field/classify_language.yml`.

```yaml
name: classify_language
description: "Classify the language of the document"
model: gpt-4o-mini
input:
  query: all_docs
  input_columns: ["raw_content"]
output_column: detected_language
schema:
  enum: ["english", "chinese", "mixed", "other"]
prompt: |
  Analyze this document and classify its primary language.
  Return one of: english, chinese, mixed, or other.
```

## Boolean field

Source: `tests/schema_examples/single_field/validate_content.yml`.

```yaml
name: validate_content
description: "Validate document content quality"
input:
  query: all_docs
  input_columns: ["raw_content", "filename"]
schema:
  content_valid: {type: "boolean"}
prompt: |
  Check if this document has valid, readable content.
  Return 'true' if the document has valid, readable content.
  Return 'false' if it's empty, corrupted, or unreadable.
```

## Full current-path config

Smoke-checked with `doctrail enrich --config config.yml packed_relevance --dry-run` and `doctrail enrich --config config.yml donor_payment --dry-run`.

```yaml
database: ./docs.db
default_model: gpt-4o-mini
default_table: documents
project: docs_smoke

sql_queries:
  all_docs: |
    SELECT rowid, sha1 FROM documents ORDER BY rowid
  donor_docs: |
    SELECT rowid, sha1 FROM documents
    WHERE raw_content LIKE '%donor%'
    ORDER BY rowid

enrichments:
  - name: packed_relevance
    input:
      query: all_docs
      input_columns: ["filename", "raw_content:240"]
    system_prompt: "Return only data matching the schema."
    append_file: context.md
    prompt: |
      Mark each item true if it mentions donor-family payment evidence.
    output_column: is_relevant
    schema: {type: "boolean"}
    dedupe_scope: query
    key_column: sha1
    pack_size: 5
    pack_response_mode: selected_indexes

  - name: donor_payment
    input:
      query: donor_docs
      input_columns: ["filename", "raw_content:500"]
    prompt: |
      Extract donor-family payment evidence from the supplied document.
      Use the filename only as source context.

      Codebook:
      - condolence_money: direct cash or ceremonial money given to a donor family.
      - relief_fund: money from a relief, hardship, charity, or assistance fund.
      - subsidy: public or institutional subsidy, reimbursement, or benefit.
      - none: no donor-family payment evidence appears.

      If payment_type is none, amount must be null and evidence should briefly say no payment evidence was found.
      If the amount is not stated, amount must be null.
    schema:
      payment_type: {enum: ["condolence_money", "relief_fund", "subsidy", "none"]}
      amount: {type: "float", minimum: 0, optional: true}
      evidence: {type: "string", maxLength: 300}
      tags: {enum_list: ["cash", "policy", "medical", "unclear"], max_items: 3}
      evidence_items:
        type: "array"
        maxItems: 3
        items:
          type: "object"
          properties:
            phrase: {type: "string", maxLength: 120}
            payment_kind: {enum: ["cash", "subsidy", "unknown"]}
```

## Prompt construction

Write `prompt` as a codebook, not just a question. Define every enum value, anchor every numeric scale point, and state gate/null behavior for optional fields. For example, if `has_payment` is false, say which evidence fields must be null; if a score runs from 0 to 5, define 0, the middle, and 5.

Keep the prompt as static as possible. Doctrail renders the prompt first, adds schema instructions there for JSON-mode paths or sends provider-native schema payloads as constant request structure, and appends per-row `input_columns` content last. OpenAI automatic prompt caching and Gemini implicit caching on Gemini 2.5 and newer models can bill repeated prefix tokens at cached-input rates when the provider reports a cache hit, though Gemini does not guarantee savings on every request. Doctrail does not currently set Anthropic `cache_control` markers.

Prefer `input_columns` with `:N` truncation, such as `raw_content:3000`, over `{column}` interpolation inside the prompt. Placeholders still work, but a row-specific token breaks the shared prompt prefix there, so everything after it re-bills at full input price per row. If one is truly needed, put it at the very end.

## Token economy pattern

Use a cheap packed pass before an expensive extraction pass when most rows are likely irrelevant. In the example above, `packed_relevance` keeps each row small with `raw_content:240`, sends five rows per request with `pack_size: 5`, and uses `pack_response_mode: selected_indexes` so the model only has to name the relevant item indexes.

That pattern is meant for triage. The packed pass writes a normalized answer for each input row, including false answers for rows the model does not select, so append mode can skip completed packed batches on rerun. Run the richer `donor_payment` extraction only on the narrowed SQL query or a saved relevance view.

## Multi-table inputs

Source: `tests/schema_examples/multi_field/multi_table_review.yml`, with its legacy `output_table` comment removed. The feature is the `table.column` input syntax.

```yaml
name: multi_table_review
description: "Review and refine extraction using data from multiple tables"
model: gpt-4o
input:
  query: docs_with_entities
  input_columns:
    - "documents.raw_content:1000"
    - "documents.filename"
    - "extracted_entities.organizations"
    - "extracted_entities.locations"
    - "extracted_entities.key_terms"
    - "extracted_entities.entity_count"
schema:
  organizations: {type: "array", items: {type: "string"}, maxItems: 10}
  locations: {type: "array", items: {type: "string", lang: "zh"}, maxItems: 5}
  key_terms: {type: "array", items: {type: "string"}, maxItems: 15}
  entity_count: {type: "integer", minimum: 0}
  quality_score: {type: "float", minimum: 0.0, maximum: 1.0}
  corrections_made: {type: "string", maxLength: 500}
prompt: |
  Review the previous entity extraction and source document supplied below.

  Use the existing entity fields as draft model output, not as ground truth.
  Correct omissions, remove spurious entities, and explain major corrections.
```

## Language validation

Source: `tests/schema_examples/advanced/test_array_language_validation.yml`.

```yaml title="fragment"
schema:
  summary_zh: {type: "string", lang: "zh"}
  evidence_en: {type: "array", items: {type: "string", lang: "en"}, maxItems: 5}
  keywords_zh: {type: "array", items: {type: "string", lang: "zh"}, maxItems: 10}
  confidence: {type: "float", minimum: 0, maximum: 1}
```

## Multiple models

Source: `tests/schema_examples/advanced/test_enrich_multi_model.yml`.

```yaml
enrichments:
  - name: compare_models
    description: "Compare outputs from multiple models"
    model: ["gpt-4o-mini", "gpt-3.5-turbo"]
    input:
      query: all_docs
      input_columns: ["raw_content:500"]
    schema:
      summary: {type: "string"}
      sentiment: {enum: ["positive", "negative", "neutral"]}
    prompt: |
      Provide a brief summary of this document and determine its sentiment.
      Summary should be one sentence only.
```

## YAML imports

Source: `tests/schema_examples/test_yaml_imports.yml`.

```yaml title="fragment"
database: placeholder
default_model: gpt-4o-mini

sql_queries:
  all_docs: |
    SELECT rowid, sha1 FROM documents
    ORDER BY rowid

enrichments:
  - !import single_field/classify_language.yml
  - !import single_field/validate_content.yml
```

## Schema surface

| Schema form | Status | Note |
| --- | --- | --- |
| `{type: "string"}` | stable | text |
| `{type: "integer"}` | stable | whole numbers |
| `{type: "float"}` | stable | decimals |
| `{type: "boolean"}` | stable | true/false |
| `{enum: [...]}` | stable | one choice |
| `{enum_list: [...]}` | stable | several choices |
| `{type: "array", items: ...}` | stable | JSON list |
| `{type: "object", properties: ...}` | stable | JSON object |
| `optional: true` | stable | allows null |
| `nullable: true` | stable | alias |
| `minimum`, `maximum` | stable | numeric bounds |
| `minLength`, `maxLength`, `pattern` | stable | string bounds |
| `minItems`, `maxItems` | stable | array bounds |
| `lang: "zh"` or `lang: "en"` | stable | language check |
| `convert: "chinese_to_pinyin"` | internal | plugin hook |
| `{type: "number"}` | deprecated | use `float` |
| `schema: [...]` | deprecated | use `enum` |

## Config key stability

Batch 4 inventory source: `.get(...)` and direct config reads across `src/doctrail/`.

| Key | Status | Note |
| --- | --- | --- |
| `database` | stable | SQLite path |
| `project` | stable | run tag |
| `default_table` | stable | source fallback |
| `default_model` | stable | model fallback |
| `sql_queries` | stable | named SQL |
| `enrichments` | stable | task list |
| `name` | stable | enrichment id |
| `input.query` | stable | named or inline SQL |
| `input.input_columns` | stable | prompt payload |
| `prompt` | stable | user prompt |
| `system_prompt` | stable | system message |
| `append_file` | stable | prompt appendix |
| `model` | stable | one or many |
| `models` | stable | model config map |
| `schema` | stable | output contract |
| `output_column` | stable | single-field alias |
| `key_column` | stable | row key |
| `dedupe_scope` | stable | query/prompt/enrichment |
| `pack_size` | stable | sync grouping |
| `pack_response_mode` | stable | exhaustive or selected_indexes |
| `output_table` | deprecated | compatibility hint |
| `dedupe-scope` | deprecated | use underscore |
| `--execution-mode openai-batch` | deprecated | use `batch` |
| `output_columns` | internal | derived/legacy |
| `all_fields_optional` | internal | broad optionality |
| `min_input_chars` | internal | skip threshold |
| `truncate` | internal | prefer CLI flag |
| `reasoning_effort` | internal | GPT-5 control |
| `exports` | internal | old export path |
| `output_naming` | internal | export filenames |
| `views.priority_columns` | internal | view sorting |
| `zotero` credentials | internal | ingest plugin |
| `documents_path` | internal | init/ingest helper |
