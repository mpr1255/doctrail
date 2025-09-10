# Schema Standardization Guide

This guide clarifies schema ambiguities and recommends best practices for Doctrail YAML schemas.

## 1. Arrays vs Enum Lists

**Array of strings** - Extract unknown values from text:
```yaml
# WHEN TO USE: Extracting entities that aren't predefined
organizations: {type: "array", items: {type: "string"}, maxItems: 10}
# Example output: ["Apple Inc.", "Microsoft", "Unknown Startup XYZ"]

key_terms: {type: "array", items: {type: "string"}, maxItems: 15}
# Example output: ["machine learning", "quantum computing", "new term not in our list"]
```

**Enum list** - Select from predefined choices:
```yaml
# WHEN TO USE: Categorizing with known options
tags:
  enum_list: ["urgent", "important", "review", "archive"]
  max_items: 3
# Example output: ["urgent", "important"] - ONLY from the list
```

## 2. Use "float" Instead of "number"

For clarity, always use `"float"`:
```yaml
# GOOD - explicit
confidence: {type: "float", minimum: 0.0, maximum: 1.0}

# AVOID - ambiguous
confidence: {type: "number", minimum: 0.0, maximum: 1.0}
```

## 3. Boolean Considerations

Booleans work but consider enums for clarity:

```yaml
# Option 1: Boolean (accepts various string representations)
has_images: {type: "boolean"}
# LLM can return: "true", "false", "yes", "no", "1", "0"
# Stored as: 0 or 1 in SQLite

# Option 2: Explicit enum (more predictable)
has_images: {enum: ["yes", "no"]}
# LLM must return exactly: "yes" or "no"
# Stored as: "yes" or "no" in SQLite
```

## 4. Always Use Explicit Enum Declaration

Avoid shorthand arrays for enums:

```yaml
# GOOD - explicit
schema:
  enum: ["positive", "negative", "neutral"]

# AVOID - implicit and confusing  
schema: ["positive", "negative", "neutral"]
```

## 5. Unique Items in Enum Lists

Enum lists automatically deduplicate by default:

```yaml
topics:
  enum_list: ["tech", "science", "health"]
  unique_items: true  # Default - duplicates auto-removed
  
# If LLM returns ["tech", "tech", "science"]
# Stored as: ["tech", "science"]
```

## 6. Constraints ARE Passed to Models

All constraints in the schema are included in the structured output request:

```yaml
summary:
  type: "string"
  minLength: 50      # ✓ Enforced by API
  maxLength: 500     # ✓ Enforced by API

score:
  type: "integer"
  minimum: 0         # ✓ Enforced by API
  maximum: 100       # ✓ Enforced by API
```

## Recommended Schema Patterns

### For Classification (Single Choice)
```yaml
# Use explicit enum
document_type:
  enum: ["email", "report", "memo", "other"]
```

### For Multi-Select Categories
```yaml
# Use enum_list for predefined options
categories:
  enum_list: ["urgent", "review", "archive", "followup"]
  min_items: 1
  max_items: 3
```

### For Entity Extraction (Unknown Values)
```yaml
# Use array when values aren't predefined
people_mentioned:
  type: "array"
  items: {type: "string"}
  maxItems: 10
```

### For Yes/No Questions
```yaml
# Option 1: Enum (recommended for clarity)
requires_followup:
  enum: ["yes", "no"]

# Option 2: Boolean (if you need SQLite INTEGER storage)
requires_followup:
  type: "boolean"
```

### For Numeric Scores
```yaml
# Use "float" not "number"
confidence_score:
  type: "float"
  minimum: 0.0
  maximum: 1.0
```

## Summary

1. **Arrays** = Extract unknown values from text
2. **Enum lists** = Select multiple from predefined choices  
3. Use **"float"** not "number" for decimals
4. Consider **enums over booleans** for yes/no questions
5. Always use **explicit enum declaration** with `enum:`
6. Constraints **are enforced** by the structured output API