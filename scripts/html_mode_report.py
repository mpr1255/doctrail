#!/usr/bin/env python3
"""Compare native HTML modes on a set of files and list repeated lines.

Run from a source checkout after `make native`:

    uv run python scripts/html_mode_report.py --paths sample.txt --profile gov.yml

Each file is extracted in article mode, in full mode, and in full mode with the
profile when one is given. The report shows failures, empty results, length,
speed, how much of the article text full mode keeps, and the lines that repeat
across the most files. Repeated lines are usually site chrome, so they are the
candidates for a profile's drop_line_patterns or drop_selectors.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

from doctrail.ingest import native_extractor

FOOTNOTE = re.compile(r"^\[?\d+\]:\s")
LINK_MARK = re.compile(r"\]?\[\d+\]?")
# Script text that article mode sometimes leaks; full mode drops it on purpose.
CODE = re.compile(r"\w\s*\(.*\)\s*;\s*$|document\.|window\.|function\s*\(|\$\(")
CHUNK = 12


def squash(text: str) -> str:
    """Letters and digits only, lowercased, so markup differences do not count."""
    return "".join(ch.lower() for ch in text if ch.isalnum())


def article_chunks(text: str) -> list[str]:
    """12-character pieces of each article line, without html2text link
    footnotes and markers or leaked script. Pieces stay inside one line, so a
    line that full mode splits or reorders still counts piece by piece."""
    chunks = []
    for line in text.splitlines():
        if FOOTNOTE.match(line.strip()):
            break
        if CODE.search(line):
            continue
        squashed = squash(LINK_MARK.sub("", line))
        chunks.extend(squashed[i : i + CHUNK] for i in range(0, len(squashed) - CHUNK + 1, CHUNK))
    return chunks


def extract(paths: list[str], html: dict, threads: int | None) -> tuple[list[dict], float]:
    started = time.perf_counter()
    docs = []
    for start in range(0, len(paths), 500):
        docs.extend(native_extractor.extract_batch(paths[start : start + 500], threads, html))
        print(f"  {html['mode']}: {len(docs)}/{len(paths)}", end="\r", file=sys.stderr)
    print(file=sys.stderr)
    return docs, time.perf_counter() - started


def summarize(name: str, docs: list[dict], seconds: float) -> None:
    lengths = [len(doc["content"]) for doc in docs if doc["content"]]
    failed = sum(doc["status"] == "failed" for doc in docs)
    empty = sum(doc["status"] != "failed" and not doc["content"] for doc in docs)
    median = statistics.median(lengths) if lengths else 0
    print(
        f"{name:<14} files {len(docs)}  failed {failed}  empty {empty}  "
        f"median chars {median:,.0f}  total chars {sum(lengths):,}  "
        f"{len(docs) / seconds:,.0f} files/s"
    )


def recall(article: list[dict], full: list[dict]) -> None:
    """Share of article text found inside the full-mode text."""
    scores = []
    for a, f in zip(article, full):
        chunks = article_chunks(a["content"])
        if not chunks:
            continue
        full_text = squash(f["content"])
        scores.append(sum(chunk in full_text for chunk in chunks) / len(chunks))
    if not scores:
        return
    scores.sort()
    print(
        f"article text kept by full mode: files {len(scores)}  "
        f"median {statistics.median(scores):.3f}  p5 {scores[len(scores) // 20]:.3f}  "
        f"files at 1.0 {sum(score == 1.0 for score in scores)}"
    )


def repeated_lines(name: str, docs: list[dict], top: int) -> None:
    """Lines that appear in the most files, each file counted once."""
    counts: Counter[str] = Counter()
    for doc in docs:
        counts.update({line.strip() for line in doc["content"].splitlines() if line.strip()})
    with_text = sum(bool(doc["content"]) for doc in docs) or 1
    print(f"\nmost repeated lines in {name} ({with_text} files with text):")
    for line, count in counts.most_common(top):
        if count < 2:
            break
        print(f"{count:>6} {count / with_text:>6.1%}  {line[:100]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--paths", type=Path, required=True, help="file with one path per line")
    parser.add_argument("--profile", type=Path, help="YAML profile of full-mode rules")
    parser.add_argument("--limit", type=int, help="use only the first N paths")
    parser.add_argument("--threads", type=int, help="native worker threads")
    parser.add_argument("--top", type=int, default=30, help="repeated lines to list")
    args = parser.parse_args()

    paths = [line.strip() for line in args.paths.read_text().splitlines() if line.strip()]
    paths = paths[: args.limit] if args.limit else paths
    configs = {"article": {"mode": "article"}, "full": {"mode": "full"}}
    if args.profile:
        rules = yaml.safe_load(args.profile.read_text(encoding="utf-8")) or {}
        configs["full+profile"] = {"mode": "full", **rules}
    configs = {name: native_extractor.html_settings(html, True) for name, html in configs.items()}

    results = {name: extract(paths, html, args.threads) for name, html in configs.items()}
    print()
    for name, (docs, seconds) in results.items():
        summarize(name, docs, seconds)
    recall(results["article"][0], results["full"][0])
    last = list(results)[-1]
    repeated_lines(last, results[last][0], args.top)


if __name__ == "__main__":
    main()
