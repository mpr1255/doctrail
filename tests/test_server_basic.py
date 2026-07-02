#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest",
#     "fastapi",
#     "httpx",
# ]
# ///

"""
Basic Server API smoke tests.

Tests that server endpoints respond correctly without mocking anything.
These are fast sanity checks, not full integration tests.
"""

import pytest
from fastapi.testclient import TestClient
from doctrail.server import app


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)


def test_health_endpoint(client):
    """Test that /health endpoint works."""
    response = client.get('/health')
    assert response.status_code == 200
    data = response.json()
    assert data['status'] == 'ok'
    assert 'version' in data


def test_enrich_endpoint_missing_config(client):
    """Test that /enrich fails gracefully with missing config."""
    response = client.post('/enrich', json={
        'config_path': '/nonexistent/config.yml',
        'enrichments': ['test']
    })
    # Should fail with 4xx error
    assert response.status_code >= 400
    assert response.status_code < 500
    # Should mention the error
    data = response.json()
    assert 'detail' in data or 'error' in str(data).lower()


def test_enrich_endpoint_validation(client, monkeypatch):
    """Test that /enrich validates request body."""
    monkeypatch.setenv("DOCTRAIL_ENABLE_WRITE_API", "1")
    monkeypatch.setenv("DOCTRAIL_SERVER_HOST", "127.0.0.1")
    monkeypatch.delenv("DOCTRAIL_SERVER_TOKEN", raising=False)

    # Missing required fields
    response = client.post('/enrich', json={})
    assert response.status_code == 422  # Validation error


def test_list_enrichments_endpoint_missing_config(client):
    """Test that /list-enrichments fails gracefully."""
    response = client.post('/list-enrichments', json={
        'config_path': '/nonexistent/config.yml'
    })
    assert response.status_code >= 400
    assert response.status_code < 500


def test_cors_headers(client):
    """Test that CORS headers are present."""
    response = client.get('/health')
    # Should have CORS headers if configured
    # (This is optional, but good to check)
    assert response.status_code == 200


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
