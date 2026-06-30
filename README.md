# Doctrail

<table align="right" border="0" cellspacing="0" cellpadding="0">
<tr><td><img src="docs/assets/mascot.png" alt="A doctrail mascot, courtesy of DALL-E 3" width="150"></td></tr>
<tr><td align="center"><sub>A doctrail mascot,<br>courtesy of DALL-E 3</sub></td></tr>
</table>

Doctrail turns document collections into auditable, structured research data with LLMs.

It ingests files into an SQLite database, applies structured codebooks and prompts to selected rows, runs them through an LLM (OpenAI, Gemini, Anthropic, or OpenRouter models), and returns ordinary tables and views for iteration and analysis.

Doctrail is meant to be driven by both researchers and coding agents.

## Start here

For researchers and users, the documentation is at <https://doctrail.dev>.

For LLMs and coding agents, the complete operating manual is at <https://doctrail.dev/llms.txt>.

## Install

Install the command line tool:

```bash
uv tool install doctrail
doctrail --help
```

Or run directly:

```bash
uvx doctrail
```

From source:

```bash
git clone https://github.com/mpr1255/doctrail
cd doctrail
uv run doctrail --help
```

## Documentation

The full documentation is at [https://doctrail.dev](https://doctrail.dev).

Inside the package, `doctrail docs` prints the reference documentation offline.

## License

MIT
