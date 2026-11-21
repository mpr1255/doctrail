# Quick start

## Installation

doctrail is built to be driven by an agent. The setup is short:

1. Install [uv](https://docs.astral.sh/uv/) if you don't have it. uv is a Python package manager. It can be installed like this:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

(In general, read install scripts before piping them into a shell; this is the official uv installer.)

2. Install doctrail: `uv tool install doctrail`

3. If you will ingest PDFs, install Poppler, which provides `pdftotext`: `brew install poppler` on macOS, `apt install poppler-utils` on Debian or Ubuntu. See [PDF text extraction](#pdf-text-extraction).

4. Tell your agent to run `doctrail` — it prints how to operate itself and points to `doctrail agent`, the full operating guide.

(Alternatively, don't install anything. Just point your agent at https://doctrail.org/llms.txt and tell it you want to install uv, doctrail, and start enriching)

The rest of this page explains what it gets you, and how to drive doctrail yourself if you prefer.

## See it work, no API key needed

Before pointing it at your own files or spending a cent, run the tutorial in a fresh empty directory:

```bash
doctrail init test
doctrail run test
```

This scaffolds a small corpus, a code book, and saved model responses into the current folder, then runs the whole pipeline offline. The [tutorial](tutorial.md) walks through exactly what just happened.

## On your own files

The assumption is simple: you are in a project folder, and your documents are in a subfolder of it.

1. Set an API key for your provider, or let `doctrail init` create a `.env` for you.
2. Run `doctrail init` in your project folder.
3. Ingest your documents, write one code book, dry-run it, run a small sample, then open the database in any SQLite browser and look at the grid.

```bash
doctrail ingest --input-dir ./data --yes
doctrail enrich <name> --dry-run
doctrail enrich <name> --limit 5
```

Each code book is a file `.doctrail/enrichments/<name>.yml` whose `name:` matches the file name. Its `input.query` names a query from `sql_queries` in `.doctrail/config.yml`, such as the `all_docs` query that `init` writes, or holds SQL directly. `doctrail new` writes a code book for you. To run `init` without prompts, as an agent would, use `doctrail init --yes --provider anthropic` (or `openai`, `gemini`, `openrouter`); it needs that provider's key in the environment or `--api-key`.

If you would rather not learn the commands, you do not have to: install doctrail, then tell your agent to run `doctrail` and order it around.

### PDF text extraction

PDF text is extracted with `pdftotext` from Poppler, which doctrail cannot bundle. Install it before ingesting PDFs: `brew install poppler` on macOS, `apt install poppler-utils` on Debian or Ubuntu. Doctrail looks for it on `PATH` and in `/opt/homebrew/bin`, `/usr/local/bin`, and `/usr/bin`; if it lives elsewhere, set `DOCTRAIL_PDFTOTEXT` to the binary. Ingest stops with these instructions when PDFs are present and `pdftotext` cannot be found.

When `pdftotext` misses spaces between words or finds too little text, doctrail tries its bundled MuPDF automatically and keeps the better reading. Pass `--pdf-engine pymupdf` to use MuPDF directly, or `--pdf-engine ocr` to send every PDF straight to OCR. Every row records which extractor produced it in `extraction_method`.

To extract a single file from Python with no database, call `doctrail.extract.extract_file(path)`: it returns the cleaned text, metadata, OCR flags, and per-page text slices for PDFs.

### Scanned documents and OCR

Ingest sends scanned PDFs and images through OCR. By default it uses local tools when they are installed: [`textra`](https://github.com/freedmand/textra) on macOS (with `mutool` for PDFs), then `ocrmypdf` for PDFs and `tesseract` for images. Doctrail does not install these tools; without one, scanned files fail extraction rather than being stored empty.

If you run your own OCR setup — a cluster, a hosted API, or a local script — plug it in without changing doctrail. Pass `--ocr-engine plugin` and set `DOCTRAIL_OCR_PLUGIN` to a Python file that defines `async def ocr_async(file_path: str) -> str` (or a synchronous `def ocr(file_path: str) -> str`):

```python
# my_ocr.py
import subprocess

def ocr(file_path: str) -> str:
    return subprocess.check_output(["my-ocr-cli", file_path], text=True)
```

```bash
DOCTRAIL_OCR_PLUGIN=./my_ocr.py doctrail ingest --input-dir ./scans --ocr-engine plugin --yes
```

Doctrail passes each scanned file's path to your function and stores the text it returns. Your plugin can be called concurrently up to `--workers`, receives both PDFs and image files (including PNGs extracted from Office documents), and is responsible for its own timeouts. Plugin mode never falls back to local OCR: when the plugin raises or returns empty text, the file is recorded as failed so a later run retries it.

Office files also have embedded images extracted and OCR'd by default. For a routine text-only refresh, use `--skip-embedded-media`; this keeps document text extraction enabled while avoiding LibreOffice conversion processes.

### Saved web pages

Much research data is web pages saved as HTML or MHTML, and doctrail tunes how it reads them more heavily than any other format. Article text sits beside menus, download links, and lists of related records, and each site lays these out differently, so the reader can be tuned to each collection.

The default keeps the main article. Choose with `--html-mode article` or `--html-mode full`:

```bash
doctrail ingest --input-dir ./pages --html-mode full --yes
```

To strip a site's menus and boilerplate, write a YAML profile of CSS selectors and line patterns and pass it with `--html-profile`. The [HTML profile reference](https://doctrail.org/html/) lists every option and shows how to build a profile from a sample. Rerun with `--overwrite` to re-extract files already ingested.

## Before real model calls

The tutorial above uses saved replay responses, so it does not need an API key. Your own enrichments do. Put the key in the project folder's `.env` file, which is usually the cleanest option:

```bash
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
GEMINI_API_KEY=...
GOOGLE_API_KEY=...
OPENROUTER_API_KEY=...
```

You only need the line for the provider you plan to use. Doctrail reads the nearest project `.env` only after a Doctrail project has been initialized, which means the same directory or an ancestor has `.doctrail/`. That project `.env` overrides keys already exported in your shell environment. A bare `.env` in an unrelated current directory is ignored, and there is no package-global Doctrail `.env`. If you want one default across projects, export it from your shell startup file such as `~/.zshenv` or `~/.bashrc`; use a project `.env` when different projects should use different providers or accounts. Do not commit `.env`; `doctrail init` adds it to `.gitignore`.

### Self-hosted models

Doctrail can use a vLLM, Ollama, or other server that exposes the OpenAI-compatible chat completions API. Point Doctrail at the server's `/v1` base URL:

```dotenv
OPENAI_COMPATIBLE_BASE_URL=http://your-gpu-server:8000/v1
# Optional when the server requires authentication:
OPENAI_COMPATIBLE_API_KEY=...
```

Prefix the exact model ID exposed by the server with `openai-compatible/`:

```yaml
default_model: openai-compatible/Qwen/Qwen3-32B
```

For a local Ollama server, the base URL is normally `http://localhost:11434/v1`. Self-hosted endpoints currently use synchronous execution; provider batch mode is not supported.

Self-hosted enrichments default to four concurrent requests and 512 output tokens. Override those limits per enrichment when the server and schema justify it, and set the served context window so `--truncate` uses the server's real limit:

```yaml
model: openai-compatible/Qwen/Qwen3-32B
concurrency: 8
max_tokens: 768
context_window: 32768
```

The endpoint may be a private URL, a local server, or a local SSH forward. For a university or HPC server bound to cluster loopback, keep vLLM private and open a local forward:

```bash
ssh -N -L 18000:127.0.0.1:8000 your-cluster
```

Then configure Doctrail normally:

```dotenv
OPENAI_COMPATIBLE_BASE_URL=http://127.0.0.1:18000/v1
```

The tunnel is only transport and access control. Doctrail still uses the standard OpenAI-compatible API, so the same configuration works with a directly reachable HTTPS endpoint.

Doctrail sends provider-native JSON schemas to vLLM and Ollama. This supports enums, booleans, bounded numbers and strings, arrays, enum lists, multi-field objects, and packed structured enrichments. The server must implement OpenAI-compatible `/v1/chat/completions` with `response_format.type=json_schema`.

Settings on an enrichment override project-level settings in `.doctrail/config.yml`. Provider batch mode remains unavailable for self-hosted endpoints; use ordinary synchronous execution, which still sends requests concurrently up to the configured limit.
