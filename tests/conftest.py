"""Pytest session configuration for the doctrail test suite.

Prevent a developer's repo-root `.env` from leaking into unit tests. This is a
plain module-level assignment rather than an autouse fixture so it never alters
monkeypatch/mocker teardown ordering in downstream test modules.
"""

import os

os.environ["DOCTRAIL_NO_SOURCE_ENV"] = "1"
