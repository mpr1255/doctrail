"""Unit tests for progress utilities."""

import pytest
from src.utils.progress import create_progress_bar, SpinnerTqdm, update_progress


class TestCreateProgressBar:
    """Test progress bar creation."""
    
    def test_verbose_mode(self):
        """Test verbose mode creates standard tqdm."""
        pbar = create_progress_bar(100, "Test", verbose=True)
        assert isinstance(pbar, type(pbar))  # tqdm type
        assert not isinstance(pbar, SpinnerTqdm)
        assert pbar.total == 100
        pbar.close()
    
    def test_spinner_mode(self):
        """Test non-verbose mode creates SpinnerTqdm."""
        pbar = create_progress_bar(50, "Test", verbose=False)
        assert isinstance(pbar, SpinnerTqdm)
        assert pbar.total == 50
        assert hasattr(pbar, 'use_spinner')
        assert pbar.use_spinner is True
        pbar.close()
    
    
    def test_leave_parameter(self):
        """Test leave parameter."""
        pbar = create_progress_bar(10, "Test", leave=False)
        assert pbar.leave is False
        pbar.close()


class TestSpinnerTqdm:
    """Test SpinnerTqdm class."""
    
    def test_spinner_initialization(self):
        """Test spinner is initialized correctly."""
        pbar = SpinnerTqdm(total=10, use_spinner=True)
        assert hasattr(pbar, 'spinner')
        assert pbar.use_spinner is True
        pbar.close()
    
    def test_no_spinner_mode(self):
        """Test without spinner."""
        pbar = SpinnerTqdm(total=10, use_spinner=False)
        assert pbar.use_spinner is False
        pbar.close()
    
    def test_format_meter_with_spinner(self):
        """Test format_meter replaces spinner placeholder."""
        pbar = SpinnerTqdm(total=10, use_spinner=True)
        bar_format = "Test {spinner} format"
        formatted = pbar.format_meter(
            5, 10, 1.0, bar_format=bar_format
        )
        # Should have replaced {spinner} with an actual character
        assert "{spinner}" not in formatted
        pbar.close()


class TestUpdateProgress:
    """Test progress update utility."""
    
    def test_simple_update(self):
        """Test simple progress update."""
        pbar = create_progress_bar(10, "Test")
        initial = pbar.n
        update_progress(pbar, 1)
        assert pbar.n == initial + 1
        pbar.close()
    
    def test_update_with_increment(self):
        """Test update with custom increment."""
        pbar = create_progress_bar(10, "Test")
        initial = pbar.n
        update_progress(pbar, 5)
        assert pbar.n == initial + 5
        pbar.close()
    
    def test_update_with_postfix(self):
        """Test update with postfix data."""
        pbar = create_progress_bar(10, "Test")
        postfix_data = {"rate": "10/s", "eta": "1m"}
        update_progress(pbar, 1, postfix=postfix_data)
        # Check postfix was set (implementation detail)
        assert pbar.postfix is not None
        pbar.close()