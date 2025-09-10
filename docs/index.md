# Doctrail Documentation

**Doctrail** is a powerful command-line tool for enriching SQLite databases using Large Language Models (LLMs). It's designed for researchers and analysts who need to process large document collections with AI-powered analysis.

## 📚 Documentation Overview

### Getting Started
- [**Quick Start Guide**](quick-start.md) - Get up and running in 5 minutes
- [**Installation**](installation.md) - Installation instructions and requirements
- [**Tutorial**](tutorial.md) - Step-by-step walkthrough of common workflows

### Core Documentation
- [**Document Ingestion**](ingestion.md) - Complete guide to importing documents (single/multiple directories)
- [**Document Enrichment**](enrichment.md) - Complete guide to AI-powered analysis (single/multi-model)
- [**CLI Reference**](cli-reference.md) - Complete command-line interface documentation
- [**Configuration Guide**](configuration.md) - YAML configuration file reference
- [**Features**](features.md) - Detailed feature documentation

### Advanced Topics
- [**Schema-Driven Enrichment**](schema-driven.md) - Using JSON schemas for structured outputs
- [**Plugin Development**](plugins.md) - Creating custom ingestion plugins
- [**Export Templates**](templates.md) - Customizing document exports

### Examples & Recipes
- [**Example Configurations**](examples.md) - Real-world configuration examples
- [**Common Recipes**](recipes.md) - Solutions for common tasks

## 🚀 Quick Example

```bash
# Ingest documents into a database
./doctrail.py ingest --input-dir ./documents --db-path ./research.db

# Enrich with AI analysis
./doctrail.py enrich --config config.yml --enrichments sentiment_analysis --limit 10

# Export results
./doctrail.py export --config config.yml --export-type report --output-dir ./results
```

## 🎯 Key Features

- **🤖 Multi-Model Support**: Works with OpenAI (GPT-4, GPT-3.5) and Google Gemini models
- **📊 Schema Validation**: Ensure structured, consistent outputs with JSON schemas
- **🔄 Incremental Processing**: Process only new or updated documents
- **💾 SQLite-Centric**: All data stored in queryable SQLite databases
- **🔌 Plugin System**: Extend with custom document ingestion plugins
- **📤 Flexible Export**: Generate reports in multiple formats (Markdown, PDF, HTML)

## 📖 Documentation Principles

This documentation follows these principles:

1. **Code-First**: Examples come from actual working configurations
2. **Task-Oriented**: Organized by what you want to accomplish
3. **Progressive Disclosure**: Start simple, add complexity as needed
4. **Real Examples**: All examples are tested and functional

## 🆘 Getting Help

- Check the [Troubleshooting Guide](troubleshooting.md)
- View [Frequently Asked Questions](faq.md)
- Report issues on [GitHub](https://github.com/yourusername/doctrail/issues)

---

*Last updated: [Auto-generated from source]*