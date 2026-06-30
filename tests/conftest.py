import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_source_env():
    """Keep the developer's real repo-root .env out of every test.

    doctrail deliberately loads <source root>/.env with override semantics, so
    without this guard any test that reasons about API-key precedence would see
    the developer's real credentials instead of its own fixtures.

    Deliberately does NOT use the monkeypatch fixture: requesting monkeypatch
    from a conftest autouse fixture hoists its setup before module-level autouse
    fixtures, which inverts monkeypatch/mocker teardown order and lets tests
    that patch the same target through both mechanisms leak mocks process-wide.
    """
    previous = os.environ.get("DOCTRAIL_NO_SOURCE_ENV")
    os.environ["DOCTRAIL_NO_SOURCE_ENV"] = "1"
    yield
    if previous is None:
        os.environ.pop("DOCTRAIL_NO_SOURCE_ENV", None)
    else:
        os.environ["DOCTRAIL_NO_SOURCE_ENV"] = previous
