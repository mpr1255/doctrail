# HTML profiles

This page is the full reference for how `doctrail ingest` reads HTML and MHTML files. The [quick start](quickstart.md#saved-web-pages) covers the common case; read this page when a corpus needs tuning.

## Modes

`--html-mode` chooses what text a page yields.

1. `article` keeps the main article and drops the rest of the page. It is the native engine's default.
2. `full` keeps all visible text, including navigation, sidebars, and footers. The native engine keeps image alt text, puts every table cell on its own line, leaves out scripts, styles, and link footnotes, and never splits a paragraph across lines, so phrase and trigram searches match across the whole paragraph. The Python engine's full mode is BeautifulSoup text, which starts a new line at every tag and drops alt text.

Without the flag, each engine keeps its default: article text in the native engine, full text in the Python engine.

In article mode, the native engine also rejects pages it identifies as login walls, HTTP error templates, link-only navigation, or leaked script. Ingest skips them and inserts no row. The Python engine has no such check. A page that carries a login box or mentions an error but also has real text is kept. The checks are heuristics: a tutorial built around code examples, or a page of short provisions that reads like a menu, can still be rejected. Full mode keeps such pages and records the reason.

Two older options affect only the Python engine. `--readability` asks for article text and conflicts with full mode. `--html-extractor smart` keeps paragraphs together instead of breaking at every tag, in both modes. When readability fails or a page needs encoding recovery, the Python engine falls back to whole-page text, and the row's `extraction_method` says which path ran.

## Profile files

A profile is a YAML mapping passed with `--html-profile FILE`, or set as `html_profile` in `.doctrail/config.yml`, where a relative path is resolved from the directory you run doctrail in. Every key is optional.

```yaml
mode: full
keep_selectors: ["#main"]
drop_selectors: ["#MapArea", ".zwjdown", "div.related > ul"]
drop_line_patterns: ["^HTML阅读$", "(?i)^share this"]
reject_low_value: false
```

1. `mode`: `article` or `full`, defaulting to `full`. `--html-mode`, or `html_mode` in the project config, overrides it. The three rule lists below need `full`.
2. `keep_selectors`: CSS selectors for the parts of the page to keep. The outermost matches left after `drop_selectors` are kept once each, in page order. If nothing matches, the whole page is kept and the row records `html_keep_selectors_matched` as 0, so a template change shows up in the metadata instead of as empty rows.
3. `drop_selectors`: CSS selectors for elements to remove, including inside kept parts. They are applied before `keep_selectors`.
4. `drop_line_patterns`: regular expressions tested against each output line after it is trimmed. A pattern matches anywhere in the line unless it is anchored with `^` and `$`. Matching lines are removed.
5. `reject_low_value`: turns the low-value check on or off. It defaults to true in article mode and false in full mode. When true, rejected pages are skipped. When false, their text is kept and `html_low_value_reason` records what was detected.

The project config also accepts `html_mode`, `readability`, `html_extractor`, and `skip_garbage_check`, with the same meaning as the flags. `--skip-garbage-check` turns off the Python engine's check for garbled encodings and the recovery that the check triggers.

Selectors follow CSS Selectors Level 3: type, class, id, attribute, descendant, child, and sibling selectors, `:not()`, and the `:nth-*` and `:first-*` pseudo-classes. `:is()`, `:where()`, and `:has()` are not supported. Patterns use the syntax of Rust's [`regex`](https://docs.rs/regex/latest/regex/#syntax) crate: inline flags such as `(?i)` and Unicode classes such as `\p{Han}` work, while lookaround and backreferences do not.

## Engines and failures

Rule lists and `reject_low_value: true` need the native engine. The Python engine accepts `mode`, ignores empty rule lists and `reject_low_value: false`, and stops with an error on anything else.

A bad selector, a bad pattern, or an unknown key stops the ingest before any file is read.

The native engine reads the first 64 KiB of each file before trusting its extension. An image saved as `.html` goes to OCR. A page saved gzip-compressed, as crawlers that store the raw HTTP response do, is decompressed and read, and the row records `content_encoding: gzip`. Audio, video, or another compressed archive is recorded as failed. A saved HTTP header before the page, as HTTrack writes, is removed when it ends within those 64 KiB.

Without rule lists, a page the native engine cannot extract, such as one nested beyond the safety limit, falls back to w3m, and an empty or timed-out MHTML page falls back to the Python engine. Fallback text is whole-page text without the native formatting. With rule lists, such a page is recorded as failed, because the fallbacks cannot apply the rules. A page that the rules leave empty is skipped and reported.

## What each row records

Each row's `metadata` JSON records `html_mode`. Native full-mode rows also record:

1. `html_config`: the effective settings as a JSON string, with defaults filled in.
2. `html_keep_selectors_matched`: how many outermost elements `keep_selectors` kept, or 0 if none matched. It is absent when the profile has no `keep_selectors`.
3. `html_nodes_dropped`: how many elements `drop_selectors` removed.
4. `html_lines_dropped`: how many lines `drop_line_patterns` removed.
5. `html_low_value_reason`: why the page looks low-value, when it does.

Their `extraction_method` is `rust:html_full` or `rust:mhtml_full`. Fallback rows record their own method and may lack these keys.

Ingest skips files already in the table, so rerun with `--overwrite` to re-extract them with a new mode or profile.

## Writing a profile

1. List a sample of the corpus's HTML files, one path per line.
2. From a source checkout with the native engine built, run `uv run python scripts/html_mode_report.py --paths files.txt`. It extracts each file in article and full mode, reports failures, empty results, length, and speed, and prints the lines repeated across the most files. Repeated lines are usually site chrome. Its retention score is the share of 12-character pieces of article text that full mode keeps; it does not check fields.
3. Turn the repeated lines into `drop_line_patterns`, or find the elements that hold them and add `drop_selectors`. If every page keeps its content in one container, name it in `keep_selectors`.
4. Rerun the report with `--profile FILE` to see what the profile leaves, and check a few pages by eye before ingesting. The retention score still compares article text with full text without the profile.

[`examples/html-profiles/cnki-detail.yml`](https://github.com/mpr1255/doctrail/blob/master/examples/html-profiles/cnki-detail.yml) is a worked profile for the older CNKI article-page layout. On 3,046 pages from one collection, it halved the median length and dropped none of the `【…】` fields. Check that the fields your study needs survive on a sample of your own pages.

Different collections usually need different profiles. Run one ingest per collection, each with its own profile.
