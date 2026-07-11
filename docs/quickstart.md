# Quick start

## Installation

doctrail is built to be driven by an agent. The setup is short:

1. Install [uv](https://docs.astral.sh/uv/) if you don't have it. uv is a Python package manager. It can be installed like this:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

(In general, read install scripts before piping them into a shell; this is the official uv installer.)

2. Install doctrail: `uv tool install doctrail`

3. Tell your agent to run `doctrail` — it prints how to operate itself and points to `doctrail agent`, the full operating guide.

(Alternatively, don't install anything. Just point your agent at https://doctrail.org/llms.txt and tell it you want to install uv, doctrail, and start enriching)

The rest of this page explains what it gets you, and how to drive doctrail yourself if you prefer.

## See it work, no API key needed

Before pointing it at your own files or spending a cent, run the tutorial:

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

If you would rather not learn the commands, you do not have to: install doctrail, then tell your agent to run `doctrail` and order it around.

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
OPENAI_COMPATIBLE_BASE_URL=http://h100:8000/v1
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
