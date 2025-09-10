"""Unit tests for enrichment service."""

import pytest
from unittest.mock import Mock, AsyncMock, patch
from src.services.enrichment_service import EnrichmentService
from src.types import EnrichmentConfig


class TestEnrichmentService:
    """Test EnrichmentService class."""
    
    @pytest.fixture
    def service(self, tmp_path):
        """Create enrichment service instance."""
        db_path = tmp_path / "test.db"
        config = {
            'default_table': 'documents',
            'default_model': 'gpt-4o-mini',
            'sql_queries': {
                'test_query': 'SELECT * FROM documents'
            }
        }
        return EnrichmentService(str(db_path), config)
    
    def test_initialization(self, service, tmp_path):
        """Test service initialization."""
        assert service.db_path == str(tmp_path / "test.db")
        assert service.config['default_table'] == 'documents'
        assert service.config['default_model'] == 'gpt-4o-mini'
    
    @patch('src.services.enrichment_service.prepare_enrichment_for_processing')
    async def test_process_enrichment_task_config_errors(self, mock_prepare, service):
        """Test handling of configuration errors."""
        # Mock config errors
        mock_strategy = Mock()
        mock_prepare.return_value = (mock_strategy, ['Error 1', 'Error 2'])
        
        enrichment_config = {'name': 'test', 'input': {'query': 'test_query'}}
        
        with pytest.raises(ValueError) as exc_info:
            await service.process_enrichment_task(enrichment_config)
        
        assert "Configuration errors: Error 1, Error 2" in str(exc_info.value)
    
    @patch('src.services.enrichment_service.ensure_output_column')
    @patch('src.services.enrichment_service.execute_query')
    @patch('src.services.enrichment_service.prepare_enrichment_for_processing')
    async def test_no_rows_to_process(self, mock_prepare, mock_execute, mock_ensure_column, service):
        """Test when no rows are returned by query."""
        # Mock successful strategy
        mock_strategy = Mock(
            input_table='documents',
            storage_mode='direct_column',
            output_columns=['result']
        )
        mock_prepare.return_value = (mock_strategy, [])
        
        # Mock empty query results
        mock_execute.return_value = []
        
        enrichment_config = {'name': 'test', 'input': {'query': 'test_query'}}
        result = await service.process_enrichment_task(enrichment_config)
        
        assert result['processed'] == 0
        assert result['message'] == "No rows to process"
    
    def test_build_query_with_rowid(self, service):
        """Test query building with specific rowid."""
        enrichment_config = {'input': {'query': 'test_query'}}
        strategy = Mock(input_table='documents')
        
        query = service._build_query(
            enrichment_config, strategy, False, None, rowid=42, sha1=None
        )
        
        assert query == "SELECT rowid, * FROM documents WHERE rowid = 42"
    
    def test_build_query_with_sha1(self, service):
        """Test query building with specific sha1."""
        enrichment_config = {'input': {'query': 'test_query'}}
        strategy = Mock(input_table='documents')
        
        query = service._build_query(
            enrichment_config, strategy, False, None, None, sha1='abc123'
        )
        
        assert query == "SELECT rowid, * FROM documents WHERE sha1 = 'abc123'"
    
    def test_build_query_with_limit(self, service):
        """Test query building with limit."""
        enrichment_config = {'input': {'query': 'test_query'}}
        strategy = Mock(
            input_table='documents',
            storage_mode='direct_column',
            output_columns=['result']
        )
        
        query = service._build_query(
            enrichment_config, strategy, False, limit=10, rowid=None, sha1=None
        )
        
        assert "LIMIT 10" in query
        assert "ORDER BY rowid" in query
    
    @patch('src.services.enrichment_service.estimate_enrichment_cost')
    @patch('src.services.enrichment_service.should_confirm_cost')
    async def test_cost_check_under_threshold(self, mock_should_confirm, mock_estimate, service):
        """Test cost check when under threshold."""
        mock_estimate.return_value = {'total_cost': 0.5}
        mock_should_confirm.return_value = False
        
        enrichment_config = {'name': 'test'}
        results = [{'rowid': 1}]
        
        cost_ok = await service._check_cost(
            results, enrichment_config, 'gpt-4o-mini', 1.0, False
        )
        
        assert cost_ok is True
        mock_estimate.assert_called_once()
        mock_should_confirm.assert_called_once()
    
    @patch('src.services.enrichment_service.estimate_enrichment_cost')
    @patch('src.services.enrichment_service.should_confirm_cost')
    @patch('src.services.enrichment_service.click.confirm')
    async def test_cost_check_needs_confirmation(
        self, mock_confirm, mock_should_confirm, mock_estimate, service
    ):
        """Test cost check when confirmation needed."""
        mock_estimate.return_value = {'total_cost': 5.0}
        mock_should_confirm.return_value = True
        mock_confirm.return_value = True
        
        enrichment_config = {'name': 'test'}
        results = [{'rowid': 1}]
        
        cost_ok = await service._check_cost(
            results, enrichment_config, 'gpt-4o-mini', 1.0, False
        )
        
        assert cost_ok is True
        mock_confirm.assert_called_once()
        assert "exceeds $1.00" in mock_confirm.call_args[0][0]