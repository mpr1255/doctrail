#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest",
#     "pytest-asyncio",
# ]
# ///
"""Test prompt template variable substitution."""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from src.llm_operations import process_row, process_row_structured

@pytest.mark.asyncio
async def test_prompt_template_substitution():
    """Test that template variables in prompts are replaced with actual column values."""
    
    # Test data
    row = {
        'rowid': 1,
        'sha1': 'test123',
        'city': 'San Francisco',
        'state': 'California',
        'population': '873965'
    }
    
    input_cols = ['city', 'state', 'population']
    parsed_input_cols = [('city', None), ('state', None), ('population', None)]
    
    # Prompt with template variables
    prompt = "The city of {city} is located in {state} and has a population of {population}."
    
    # Mock objects
    semaphore = asyncio.Semaphore(1)
    pbar = Mock()
    pbar.update = Mock()
    
    # Mock the call_llm function to capture the actual prompt sent
    captured_messages = []
    
    async def mock_call_llm(model, messages, system_prompt, verbose):
        captured_messages.extend(messages)
        return "Test response"
    
    with patch('src.llm_operations.call_llm', side_effect=mock_call_llm):
        result = await process_row(
            row=row,
            input_cols=input_cols,
            parsed_input_cols=parsed_input_cols,
            prompt=prompt,
            model='gpt-4o-mini',
            semaphore=semaphore,
            pbar=pbar,
            output_col='result',
            output_schema=None,
            system_prompt=None,
            config=None,
            truncate=False,
            verbose=False
        )
    
    # Check that the template variables were replaced in the prompt
    assert len(captured_messages) == 1
    user_message = captured_messages[0]['content']
    
    # The prompt should have template variables replaced
    assert "The city of San Francisco is located in California and has a population of 873965." in user_message
    assert "{city}" not in user_message
    assert "{state}" not in user_message
    assert "{population}" not in user_message


@pytest.mark.asyncio
async def test_prompt_template_with_table_prefix():
    """Test template substitution with table.column syntax."""
    
    # Test data with table prefixes
    row = {
        'rowid': 1,
        'sha1': 'test123',
        'title': 'Research Paper',
        'sentiment': 'positive',
        'confidence': '0.85'
    }
    
    input_cols = ['documents.title', 'analysis.sentiment', 'analysis.confidence']
    parsed_input_cols = [('documents.title', None), ('analysis.sentiment', None), ('analysis.confidence', None)]
    
    # Prompt with both table.column and plain column template variables
    prompt = "Document: {documents.title} has {sentiment} sentiment with {confidence} confidence."
    
    # Mock objects
    semaphore = asyncio.Semaphore(1)
    pbar = Mock()
    pbar.update = Mock()
    
    # Mock the call_llm function
    captured_messages = []
    
    async def mock_call_llm(model, messages, system_prompt, verbose):
        captured_messages.extend(messages)
        return "Test response"
    
    # Mock apply_column_limits to simulate the column resolution
    def mock_apply_column_limits(row_data, parsed_columns):
        # Simulate how apply_column_limits resolves table.column names
        result = {}
        for col_name, _ in parsed_columns:
            if col_name == 'documents.title':
                result[col_name] = row_data.get('title', '')
            elif col_name == 'analysis.sentiment':
                result[col_name] = row_data.get('sentiment', '')
            elif col_name == 'analysis.confidence':
                result[col_name] = row_data.get('confidence', '')
        return result
    
    with patch('src.llm_operations.call_llm', side_effect=mock_call_llm), \
         patch('src.llm_operations.apply_column_limits', side_effect=mock_apply_column_limits):
        
        result = await process_row(
            row=row,
            input_cols=input_cols,
            parsed_input_cols=parsed_input_cols,
            prompt=prompt,
            model='gpt-4o-mini',
            semaphore=semaphore,
            pbar=pbar,
            output_col='result',
            output_schema=None,
            system_prompt=None,
            config=None,
            truncate=False,
            verbose=False
        )
    
    # Check the prompt was properly templated
    assert len(captured_messages) == 1
    user_message = captured_messages[0]['content']
    
    # Should have replaced all template variables
    assert "Document: Research Paper has positive sentiment with 0.85 confidence." in user_message
    assert "{documents.title}" not in user_message
    assert "{sentiment}" not in user_message
    assert "{confidence}" not in user_message


if __name__ == "__main__":
    pytest.main([__file__, "-v"])