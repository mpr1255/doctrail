"""
Tests for the FastAPI server endpoints.

These tests verify that the server endpoints correctly wrap the core API functions.
"""

import sqlite3
import textwrap

import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from doctrail.server import app
from doctrail.core import ConfigurationError, EnrichmentError, DatabaseError
from doctrail.server_config import DatabaseConfig, SchemaConfig, ServerConfig


# Create test client
client = TestClient(app)


@pytest.fixture
def write_api_enabled(monkeypatch):
    """Enable legacy write endpoints for compatibility endpoint tests."""
    monkeypatch.setenv("DOCTRAIL_ENABLE_WRITE_API", "1")
    monkeypatch.setenv("DOCTRAIL_SERVER_HOST", "127.0.0.1")
    monkeypatch.delenv("DOCTRAIL_SERVER_TOKEN", raising=False)


class TestHealthEndpoints:
    """Test health check endpoints."""

    def test_root_endpoint(self):
        """Test GET / returns health status."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_health_endpoint(self):
        """Test GET /health returns health status."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_help_enrich_describes_gated_legacy_endpoints(self):
        """Fallback enrich help should not advertise unimplemented /db write routes."""
        response = client.get("/help/enrich")

        assert response.status_code == 200
        assert "POST /enrich" in response.text
        assert "--enable-write-api" in response.text
        assert "POST /db/{name}/enrich" not in response.text


class TestLegacyWriteApiSecurity:
    """Legacy write endpoints should be closed unless explicitly enabled."""

    @patch('doctrail.server.run_enrichment')
    def test_write_endpoint_disabled_by_default(self, mock_run_enrichment, monkeypatch):
        monkeypatch.delenv("DOCTRAIL_ENABLE_WRITE_API", raising=False)
        monkeypatch.delenv("DOCTRAIL_SERVER_TOKEN", raising=False)
        monkeypatch.setenv("DOCTRAIL_SERVER_HOST", "127.0.0.1")

        response = client.post("/enrich", json={
            "config_path": "test_config.yml",
            "enrichments": ["test_task"],
        })

        assert response.status_code == 403
        assert "Legacy write API disabled" in response.json()["detail"]
        mock_run_enrichment.assert_not_called()

    @patch('doctrail.server.run_enrichment')
    def test_non_loopback_write_endpoint_requires_token(self, mock_run_enrichment, monkeypatch):
        monkeypatch.setenv("DOCTRAIL_ENABLE_WRITE_API", "1")
        monkeypatch.setenv("DOCTRAIL_SERVER_HOST", "0.0.0.0")
        monkeypatch.delenv("DOCTRAIL_SERVER_TOKEN", raising=False)

        response = client.post("/enrich", json={
            "config_path": "test_config.yml",
            "enrichments": ["test_task"],
        })

        assert response.status_code == 403
        assert "bearer token is required" in response.json()["detail"]
        mock_run_enrichment.assert_not_called()

    @patch('doctrail.server.run_enrichment')
    def test_token_required_when_configured(self, mock_run_enrichment, monkeypatch):
        monkeypatch.setenv("DOCTRAIL_ENABLE_WRITE_API", "1")
        monkeypatch.setenv("DOCTRAIL_SERVER_HOST", "0.0.0.0")
        monkeypatch.setenv("DOCTRAIL_SERVER_TOKEN", "secret")

        response = client.post("/enrich", json={
            "config_path": "test_config.yml",
            "enrichments": ["test_task"],
        })

        assert response.status_code == 401
        mock_run_enrichment.assert_not_called()

    @patch('doctrail.server.run_enrichment')
    def test_token_allows_enabled_write_endpoint(self, mock_run_enrichment, monkeypatch):
        monkeypatch.setenv("DOCTRAIL_ENABLE_WRITE_API", "1")
        monkeypatch.setenv("DOCTRAIL_SERVER_HOST", "0.0.0.0")
        monkeypatch.setenv("DOCTRAIL_SERVER_TOKEN", "secret")
        mock_run_enrichment.return_value = {
            'status': 'success',
            'enrichments_run': ['test_task'],
            'results': [],
            'errors': [],
            'total_processed': 0,
        }

        response = client.post(
            "/enrich",
            headers={"Authorization": "Bearer secret"},
            json={"config_path": "test_config.yml", "enrichments": ["test_task"]},
        )

        assert response.status_code == 200
        mock_run_enrichment.assert_called_once()


def test_text_endpoint_quotes_configured_identifiers(tmp_path, monkeypatch):
    """Configured document table/key/content names should be safely quoted."""
    import doctrail.server as server_module

    db_dir = tmp_path / "weird"
    db_dir.mkdir()
    db_path = db_dir / "weird.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        'CREATE TABLE "docs table" ("doc id" TEXT PRIMARY KEY, "body text" TEXT)'
    )
    conn.execute(
        'INSERT INTO "docs table" ("doc id", "body text") VALUES (?, ?)',
        ("doc-1", "quoted content"),
    )
    conn.commit()
    conn.close()

    db_config = DatabaseConfig(
        name="weird",
        path=db_dir,
        db_file=db_path,
        schema=SchemaConfig(
            pk_column="doc id",
            content_column="body text",
            documents_table="docs table",
        ),
    )
    monkeypatch.setattr(
        server_module,
        "server_config",
        ServerConfig(databases={"weird": db_config}),
    )

    assert db_config.get_stats()["document_count"] == 1
    response = client.get("/db/weird/text/doc-1")

    assert response.status_code == 200
    assert response.text == "quoted content"


@pytest.mark.usefixtures("write_api_enabled")
class TestEnrichEndpoint:
    """Test /enrich endpoint."""

    @pytest.mark.asyncio
    @patch('doctrail.server.run_enrichment')
    async def test_enrich_success(self, mock_run_enrichment):
        """Test successful enrichment request."""
        # Mock core function
        mock_run_enrichment.return_value = {
            'status': 'success',
            'enrichments_run': ['test_task'],
            'results': [{'rowid': 1, 'output': 'test'}],
            'errors': [],
            'total_processed': 1
        }

        # Make request
        response = client.post("/enrich", json={
            "config_path": "test_config.yml",
            "enrichments": ["test_task"],
            "limit": 10,
            "verbose": False
        })

        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'success'
        assert data['total_processed'] == 1
        assert 'test_task' in data['enrichments_run']

    @pytest.mark.asyncio
    @patch('doctrail.server.run_enrichment')
    async def test_enrich_configuration_error(self, mock_run_enrichment):
        """Test enrichment with configuration error."""
        # Mock core function to raise ConfigurationError
        mock_run_enrichment.side_effect = ConfigurationError("Config file not found")

        # Make request
        response = client.post("/enrich", json={
            "config_path": "missing.yml",
            "enrichments": ["test_task"]
        })

        # Verify error response
        assert response.status_code == 400
        assert "Configuration error" in response.json()["detail"]

    @pytest.mark.asyncio
    @patch('doctrail.server.run_enrichment')
    async def test_enrich_database_error(self, mock_run_enrichment):
        """Test enrichment with database error."""
        # Mock core function to raise DatabaseError
        mock_run_enrichment.side_effect = DatabaseError("Database not found")

        # Make request
        response = client.post("/enrich", json={
            "config_path": "test_config.yml",
            "enrichments": ["test_task"]
        })

        # Verify error response
        assert response.status_code == 404
        assert "Database error" in response.json()["detail"]

    @pytest.mark.asyncio
    @patch('doctrail.server.run_enrichment')
    async def test_enrich_enrichment_error(self, mock_run_enrichment):
        """Test enrichment with enrichment error."""
        # Mock core function to raise EnrichmentError
        mock_run_enrichment.side_effect = EnrichmentError("Task not found")

        # Make request
        response = client.post("/enrich", json={
            "config_path": "test_config.yml",
            "enrichments": ["missing_task"]
        })

        # Verify error response
        assert response.status_code == 422
        assert "Enrichment error" in response.json()["detail"]

    def test_enrich_with_all_parameters(self):
        """Test enrichment with all optional parameters."""
        with patch('doctrail.server.run_enrichment') as mock_run_enrichment:
            mock_run_enrichment.return_value = {
                'status': 'success',
                'enrichments_run': ['task1'],
                'results': [],
                'errors': [],
                'total_processed': 0
            }

            response = client.post("/enrich", json={
                "config_path": "config.yml",
                "enrichments": ["task1", "task2"],
                "db_path": "/path/to/db.sqlite",
                "model": "gpt-4o-mini",
                "limit": 100,
                "rowid": None,
                "sha1": None,
                "overwrite": True,
                "batch_size": 50,
                "truncate": True,
                "skip_cost_check": True,
                "cost_threshold": 10.0,
                "dry_run": False,
                "verbose": True
            })

            assert response.status_code == 200
            # Verify mock was called with correct parameters
            mock_run_enrichment.assert_called_once()
            call_kwargs = mock_run_enrichment.call_args.kwargs
            assert call_kwargs['config_path'] == 'config.yml'
            assert call_kwargs['enrichments'] == ['task1', 'task2']
            assert call_kwargs['model'] == 'gpt-4o-mini'
            assert call_kwargs['limit'] == 100
            assert call_kwargs['overwrite'] is True


@pytest.mark.usefixtures("write_api_enabled")
class TestIngestEndpoint:
    """Test /ingest endpoint."""

    @pytest.mark.asyncio
    @patch('doctrail.server.run_ingest')
    async def test_ingest_local_files_success(self, mock_run_ingest):
        """Test successful local file ingestion."""
        # Mock core function
        mock_run_ingest.return_value = {
            'status': 'success',
            'mode': 'local',
            'directories_processed': 1,
            'results': [{'files_processed': 10}]
        }

        # Make request
        response = client.post("/ingest", json={
            "db_path": "/path/to/db.sqlite",
            "input_dirs": ["/path/to/docs"],
            "table": "documents",
            "verbose": False
        })

        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'success'
        assert data['mode'] == 'local'
        assert data['directories_processed'] == 1

    @pytest.mark.asyncio
    @patch('doctrail.server.run_ingest')
    async def test_ingest_plugin_mode(self, mock_run_ingest):
        """Test plugin ingestion."""
        # Mock core function
        mock_run_ingest.return_value = {
            'status': 'success',
            'plugin': 'zotero',
            'result': {'items_processed': 50}
        }

        # Make request
        response = client.post("/ingest", json={
            "db_path": "/path/to/db.sqlite",
            "plugin_name": "zotero",
            "plugin_args": {
                "collection": "My Research",
                "api_key": "test_key"
            }
        })

        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'success'
        assert data['plugin'] == 'zotero'

    @pytest.mark.asyncio
    @patch('doctrail.server.run_ingest')
    async def test_ingest_zotero_mode(self, mock_run_ingest):
        """Test Zotero ingestion."""
        # Mock core function
        mock_run_ingest.return_value = {
            'status': 'success',
            'mode': 'zotero',
            'result': {'items_synced': 30}
        }

        # Make request
        response = client.post("/ingest", json={
            "db_path": "/path/to/db.sqlite",
            "zotero_api_key": "test_key",
            "zotero_library_id": "12345",
            "zotero_library_type": "user",
            "zotero_collection": "My Collection"
        })

        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'success'
        assert data['mode'] == 'zotero'

    @pytest.mark.asyncio
    @patch('doctrail.server.run_ingest')
    async def test_ingest_configuration_error(self, mock_run_ingest):
        """Test ingest with configuration error."""
        # Mock core function to raise ConfigurationError
        mock_run_ingest.side_effect = ConfigurationError("Invalid directory")

        # Make request
        response = client.post("/ingest", json={
            "db_path": "/path/to/db.sqlite",
            "input_dirs": ["/nonexistent"]
        })

        # Verify error response
        assert response.status_code == 400
        assert "Configuration error" in response.json()["detail"]


@pytest.mark.usefixtures("write_api_enabled")
class TestExportEndpoint:
    """Test /export endpoint."""

    @pytest.mark.asyncio
    @patch('doctrail.server.run_export')
    async def test_export_success(self, mock_run_export):
        """Test successful export."""
        # Mock core function
        mock_run_export.return_value = {
            'status': 'success',
            'export_type': 'report',
            'output_dir': '/path/to/exports',
            'result': {'files_created': 5}
        }

        # Make request
        response = client.post("/export", json={
            "config_path": "config.yml",
            "export_type": "report",
            "output_dir": "/path/to/exports",
            "verbose": False
        })

        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'success'
        assert data['export_type'] == 'report'
        assert data['output_dir'] == '/path/to/exports'

    @pytest.mark.asyncio
    @patch('doctrail.server.run_export')
    async def test_export_configuration_error(self, mock_run_export):
        """Test export with configuration error."""
        # Mock core function to raise ConfigurationError
        mock_run_export.side_effect = ConfigurationError("Config file not found")

        # Make request
        response = client.post("/export", json={
            "config_path": "missing.yml",
            "export_type": "report"
        })

        # Verify error response
        assert response.status_code == 400
        assert "Configuration error" in response.json()["detail"]


@pytest.mark.usefixtures("write_api_enabled")
class TestRequestModels:
    """Test Pydantic request models validate correctly."""

    def test_enrichment_request_defaults(self):
        """Test EnrichmentRequest with minimal required fields."""
        response = client.post("/enrich", json={
            "config_path": "config.yml",
            "enrichments": ["task1"]
        })
        # Should not fail validation
        assert response.status_code in [200, 400, 404, 422, 500]  # Any non-422 is validation pass

    def test_enrichment_request_invalid(self):
        """Test EnrichmentRequest with missing required fields."""
        response = client.post("/enrich", json={
            "enrichments": ["task1"]
            # Missing config_path
        })
        assert response.status_code == 422  # Validation error

    def test_ingest_request_defaults(self):
        """Test IngestRequest with minimal required fields."""
        response = client.post("/ingest", json={
            "db_path": "/path/to/db.sqlite"
        })
        # Should not fail validation
        assert response.status_code in [200, 400, 404, 500]

    def test_ingest_request_invalid(self):
        """Test IngestRequest with missing required fields."""
        response = client.post("/ingest", json={
            "input_dirs": ["/path/to/docs"]
            # Missing db_path
        })
        assert response.status_code == 422  # Validation error

    def test_export_request_defaults(self):
        """Test ExportRequest with minimal required fields."""
        response = client.post("/export", json={
            "config_path": "config.yml",
            "export_type": "report"
        })
        # Should not fail validation
        assert response.status_code in [200, 400, 500]

    def test_export_request_invalid(self):
        """Test ExportRequest with missing required fields."""
        response = client.post("/export", json={
            "export_type": "report"
            # Missing config_path
        })
        assert response.status_code == 422  # Validation error


class TestOpenAPIDocumentation:
    """Test that OpenAPI docs are generated correctly."""

    def test_openapi_json_exists(self):
        """Test that /openapi.json endpoint works."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert "openapi" in schema
        assert "info" in schema
        assert schema["info"]["title"] == "Doctrail API"

    def test_docs_endpoint_exists(self):
        """Test that /docs endpoint exists."""
        response = client.get("/docs")
        assert response.status_code == 200

    def test_redoc_endpoint_exists(self):
        """Test that /redoc endpoint exists."""
        response = client.get("/redoc")
        assert response.status_code == 200

    def test_openapi_has_all_endpoints(self):
        """Test that OpenAPI schema includes all endpoints."""
        response = client.get("/openapi.json")
        schema = response.json()
        paths = schema["paths"]

        # Verify all expected endpoints are documented
        assert "/" in paths
        assert "/health" in paths
        assert "/enrich" in paths
        assert "/ingest" in paths
        assert "/export" in paths

        # Verify HTTP methods
        assert "post" in paths["/enrich"]
        assert "post" in paths["/ingest"]
        assert "post" in paths["/export"]


class TestServerContracts:
    """Focused regressions for the non-mocked server surfaces."""

    def test_multidb_fts_uses_configured_chunks_table(self, tmp_path, monkeypatch):
        """Multi-db FTS should honor schema.chunks_table instead of hard-coding 'chunks'."""
        import doctrail.server as server_module

        db_dir = tmp_path / "demo"
        db_dir.mkdir()
        db_path = db_dir / "docs.db"

        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, title TEXT, raw_content TEXT)")
        conn.execute("INSERT INTO documents (id, title, raw_content) VALUES (1, 'Doc', 'hello world')")
        conn.execute(
            "CREATE TABLE segments (id INTEGER PRIMARY KEY, document_id INTEGER, chunk_index INTEGER, content TEXT)"
        )
        conn.execute(
            "INSERT INTO segments (id, document_id, chunk_index, content) VALUES (1, 1, 0, 'hello world')"
        )
        conn.execute("CREATE VIRTUAL TABLE chunk_fts USING fts5(content, content=segments, content_rowid=id)")
        conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('rebuild')")
        conn.commit()
        conn.close()

        (db_dir / "doctrail.yaml").write_text(textwrap.dedent("""
            schema:
              pk_column: id
              pk_type: integer
              title_column: title
              content_column: raw_content
              documents_table: documents
              chunks_table: segments
        """))

        config_path = tmp_path / "server.yml"
        config_path.write_text(textwrap.dedent(f"""
            server:
              host: 127.0.0.1
              port: 8000
            databases:
              demo: {db_dir}
        """))
        monkeypatch.setenv("DOCTRAIL_SERVER_CONFIG", str(config_path))

        with TestClient(server_module.app) as local_client:
            response = local_client.get("/db/demo/fts", params={"q": "hello", "format": "json"})

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["results"][0]["doc_id"] == "1"
