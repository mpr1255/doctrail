"""Backward-compatible cost estimation module.

This module re-exports the public interface from `doctrail.utils.cost_estimation`
so older code and tests that import `doctrail.cost_estimation` continue to work.
"""

from .utils.cost_estimation import (  # noqa: F401
    MODEL_ENCODINGS,
    count_tokens,
    estimate_enrichment_cost,
    estimate_output_tokens,
    format_cost_estimate,
    should_confirm_cost,
    get_model_validation_error,
    get_provider_models,
    validate_model,
    get_supported_models,
    get_encoding_for_model,
)

__all__ = [
    "MODEL_ENCODINGS",
    "count_tokens",
    "estimate_enrichment_cost",
    "estimate_output_tokens",
    "format_cost_estimate",
    "should_confirm_cost",
    "get_model_validation_error",
    "get_provider_models",
    "validate_model",
    "get_supported_models",
    "get_encoding_for_model",
]
