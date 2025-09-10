# XML Schema Guide for Doctrail

## Overview

Doctrail supports XML-based extraction with declarative schema definitions. Define your XML structure once in YAML, and the system automatically:

1. Generates XML templates for LLM prompts
2. Creates SQL tables and columns
3. Parses XML responses into structured data
4. Updates multiple related tables

## Basic Example

```yaml
# Define XML schemas
xml_schemas:
  person_extraction:
    root: document
    elements:
      # Single-value fields (go to parent table)
      doc_type:
        type: text
        sql_type: TEXT
      date:
        type: text
        sql_type: TEXT
      
      # Multi-value field (creates child table)
      person:
        type: array
        table: extracted_persons
        elements:
          name:
            type: text
            sql_type: TEXT
          role:
            type: enum
            values: [plaintiff, defendant, witness, judge, attorney, other]
            sql_type: TEXT
          age:
            type: integer
            sql_type: INTEGER

# Use in enrichment
enrichments:
  - name: extract_persons
    table: documents
    input:
      query: "SELECT rowid, sha1, * FROM documents"
      input_columns: ["content"]
    output_format: xml
    xml_schema: person_extraction
    prompt_append_template: true
    prompt: "Extract all persons mentioned in this document."
```

## Schema Definition

### Structure
```yaml
xml_schemas:
  schema_name:
    root: root_element_name
    elements:
      element_name:
        type: text|integer|numeric|enum|array
        sql_type: TEXT|INTEGER|REAL
        values: [...]  # For enum type
        table: child_table_name  # For array type
        elements: {...}  # For array type
```

### Field Types

- **text**: String values → SQL TEXT
- **integer**: Whole numbers → SQL INTEGER  
- **numeric**: Decimal numbers → SQL REAL
- **enum**: Predefined values → SQL TEXT
- **array**: Multiple occurrences → Separate table

## Generated Outputs

### 1. XML Template
The system generates a template with helpful comments:

```xml
<!-- Template: person_extraction -->
<!-- OUTPUT REQUIREMENT: Return ONLY well-formed UTF-8 XML, no markdown or wrappers -->

<document>
  <doc_type></doc_type>
  <date></date>
  <!-- Repeat this block for each person -->
  <person>
    <!-- name: text -->
    <name></name>
    <!-- role: plaintiff | defendant | witness | judge | attorney | other -->
    <role></role>
    <!-- age: numeric value -->
    <age></age>
  </person>
</document>
```

### 2. SQL Schema
Automatically generates:

```sql
-- Parent table updates
ALTER TABLE documents ADD COLUMN doc_type TEXT;
ALTER TABLE documents ADD COLUMN date TEXT;
ALTER TABLE documents ADD COLUMN extraction_xml TEXT;
ALTER TABLE documents ADD COLUMN extraction_ts TEXT;

-- Child table creation
CREATE TABLE IF NOT EXISTS extracted_persons (
    extracted_persons_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_sha1 TEXT NOT NULL,
    name TEXT,
    role TEXT,
    age INTEGER,
    FOREIGN KEY(doc_sha1) REFERENCES documents(sha1)
);
CREATE INDEX IF NOT EXISTS idx_extracted_persons_doc ON extracted_persons(doc_sha1);
```

## Advanced Features

### Nested Arrays
Support for nested structures (e.g., evidence with translations):

```yaml
mechanism:
  type: array
  table: compensation_mechanisms
  elements:
    category:
      type: text
      sql_type: TEXT
    evidence:
      type: array
      elements:
        cn:
          type: text
          sql_type: TEXT
        en:
          type: text
          sql_type: TEXT
```

This flattens to columns: `evidence_cn` and `evidence_en`

### Enum Validation
Define allowed values for consistent extraction:

```yaml
doc_type:
  type: enum
  values: [policy, news, academic, legal, other]
  sql_type: TEXT
```

## Usage in Enrichments

```yaml
enrichments:
  - name: my_extraction
    table: documents
    input:
      query: "SELECT * FROM documents WHERE processed = 0"
      input_columns: ["content"]
    
    # XML-specific settings
    output_format: xml
    xml_schema: my_schema_name
    prompt_append_template: true  # Append XML template to prompt
    
    # Standard enrichment settings
    model: gpt-4o
    prompt: "Extract information according to the template."
```

## Processing Flow

1. **Query Execution**: Retrieves documents based on input query
2. **Template Generation**: Creates XML template from schema
3. **LLM Call**: Sends prompt + template + content to model
4. **XML Parsing**: Validates and parses response
5. **Database Updates**: 
   - Updates parent table columns
   - Inserts child table records
   - Stores raw XML for audit

## Best Practices

1. **Schema Design**
   - Keep single-value fields in parent table
   - Use child tables for one-to-many relationships
   - Define appropriate SQL types

2. **Prompt Engineering**
   - Use `prompt_append_template: true` for clarity
   - Include clear instructions about XML format
   - Specify that only XML should be returned

3. **Error Handling**
   - System validates XML structure
   - Stores raw XML for debugging
   - Logs parsing errors

4. **Performance**
   - Use focused queries to limit rows processed
   - Consider two-pass approach: filter first, extract second
   - Monitor child table growth

## Complete Example: Document Analysis

```yaml
xml_schemas:
  legal_document_analysis:
    root: analysis
    elements:
      case_type:
        type: enum
        values: [civil, criminal, administrative, other]
        sql_type: TEXT
      jurisdiction:
        type: text
        sql_type: TEXT
      decision_date:
        type: text
        sql_type: TEXT
      
      party:
        type: array
        table: case_parties
        elements:
          name:
            type: text
            sql_type: TEXT
          party_type:
            type: enum
            values: [plaintiff, defendant, third_party]
            sql_type: TEXT
          represented_by:
            type: text
            sql_type: TEXT
      
      legal_issue:
        type: array
        table: legal_issues
        elements:
          category:
            type: text
            sql_type: TEXT
          description:
            type: text
            sql_type: TEXT
          ruling:
            type: enum
            values: [upheld, rejected, remanded, other]
            sql_type: TEXT

enrichments:
  - name: analyze_legal_docs
    table: documents
    input:
      query: "SELECT * FROM documents WHERE doc_type = 'legal'"
      input_columns: ["content", "title"]
    output_format: xml
    xml_schema: legal_document_analysis
    prompt_append_template: true
    model: gpt-4o
    prompt: |
      You are a legal analyst. Extract key information from this legal document.
      Focus on parties involved, legal issues, and case outcomes.
```

This creates a rich, queryable structure for legal document analysis with proper relationships between entities.