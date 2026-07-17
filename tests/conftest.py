"""Pytest session configuration for the doctrail test suite.

The compiled Rust extraction extension (``doctrail._ingest_native``) is an
optional accelerator. A local dev build may drop the ``.so`` into the package,
but CI has none. Force the Python extraction path for the whole suite so results
are deterministic and identical on every machine, regardless of whether the
extension happens to be built. ``test_native_extractor.py`` clears this locally
to exercise the Rust path when the extension is present.

This is a plain module-level assignment, not an autouse fixture: a conftest
autouse fixture requesting ``monkeypatch`` would hoist its setup ahead of
module-level autouse fixtures and invert monkeypatch/mocker teardown order.
"""

import os

os.environ["DOCTRAIL_DISABLE_NATIVE"] = "1"
