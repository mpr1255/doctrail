# Optional Fields in Schema-Driven Enrichments

## Problem

When using schema-driven enrichments with structured outputs, all fields were marked as required by default. This caused the LLM to generate explanatory text like "No evidence of cash payments found" instead of returning `null` values when information wasn't present in the document.

## Solution

We've added support for optional fields in schema definitions. There are two ways to make fields optional:

### 1. Individual Optional Fields

Add `optional: true` to specific field definitions in your schema:

```yaml
enrichments:
  - name: cash_payment_analysis
    description: "Analyze cash payments in documents"
    output_table: cash_payments
    schema:
      payment_type:
        enum: ["direct", "indirect", "none"]
        optional: true  # This field can be null
      payment_amount:
        type: "number"
        minimum: 0
        optional: true  # This field can be null
      payment_details:
        type: "string"
        optional: true  # This field can be null
```

### 2. All Fields Optional by Default

Add `all_fields_optional: true` at the enrichment level to make all schema fields optional:

```yaml
enrichments:
  - name: document_analysis
    description: "Extract various aspects from documents"
    output_table: analysis_results
    all_fields_optional: true  # All fields in schema will be optional
    schema:
      sentiment: {enum: ["positive", "negative", "neutral"]}
      key_topics: {type: "array", items: {type: "string"}}
      summary: {type: "string", maxLength: 500}
```

### 3. Alternative: nullable

You can also use `nullable: true` as an alias for `optional: true`:

```yaml
schema:
  compensation_amount:
    type: "number"
    nullable: true  # Same as optional: true
```

## How It Works

When a field is marked as optional:
- The Pydantic model wraps the field type in `Optional[Type]`
- The field gets a default value of `None`
- The LLM can return `null` for that field when no relevant information is found
- The database will store NULL values instead of placeholder text

## Example Usage

For a document that mentions indirect benefits but no cash payments:

**Before (all fields required):**
```json
{
  "payment_type": "indirect",
  "payment_amount": "No amount specified in document",
  "payment_details": "Document mentions priority medical access"
}
```

**After (with optional fields):**
```json
{
  "payment_type": "indirect",
  "payment_amount": null,
  "payment_details": "Document mentions priority medical access"
}
```

## Benefits

1. **Cleaner Data**: No more explanatory text in fields meant for specific values
2. **Better Analysis**: Easy to filter for documents with/without specific information
3. **LLM Efficiency**: The model doesn't need to generate placeholder text
4. **Database Queries**: Can use SQL `IS NULL` checks effectively

## Migration

For existing enrichments, add `optional: true` to fields that should allow null values, or add `all_fields_optional: true` at the enrichment level if most fields should be optional.