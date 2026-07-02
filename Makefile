.PHONY: help generated docs docs-fresh generated-clean package-docs-check test deploy release-check

help:
	@printf "%s\n" \
		"make generated           Rebuild generated docs artifacts." \
		"make docs                Rebuild generated docs artifacts and the MkDocs site." \
		"make docs-fresh          Verify generated docs match the current worktree." \
		"make generated-clean     Fail if committed generated docs are stale." \
		"make package-docs-check  Verify doctrail docs/skill print packaged docs." \
		"make test                Run the non-integration test suite." \
		"make deploy              Prepare the tree for the GitHub Pages deploy workflow." \
		"make release-check       Run the generated-docs gates for a tagged release."

generated:
	uv run python scripts/build_llms_full.py

docs: generated
	uv run --with mkdocs-material --with mkdocs-click mkdocs build --strict

docs-fresh:
	uv run python -m pytest tests/test_docs_drift.py -q

generated-clean: generated
	git diff --exit-code docs/llms.txt

package-docs-check:
	uv run doctrail docs > /tmp/doctrail-llms.txt
	cmp docs/llms.txt /tmp/doctrail-llms.txt
	uv run doctrail skill > /tmp/doctrail-skill.txt
	cmp skills/doctrail/SKILL.md /tmp/doctrail-skill.txt

test:
	uv run python -m pytest tests/ -x -q -k "not integration"

deploy: docs package-docs-check test
	git status --short

release-check: generated-clean docs package-docs-check test
