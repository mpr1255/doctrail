#!/usr/bin/env python3
"""Convenience entry point for doctrail test runs."""

from tests.doctrail_support import *


def run_all_tests(verbose=False):
    """Run all tests and return results."""
    args = ["-v"] if verbose else []
    
    # Add coverage if available
    try:
        import pytest_cov
        args.extend(["--cov=doctrail", "--cov-report=term-missing"])
    except ImportError:
        pass
    
    # Run tests
    return pytest.main([__file__] + args)
