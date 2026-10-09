import pytest
from pydantic import ValidationError

from doctrail.pydantic_schema import create_pydantic_model_from_schema


def test_string_pattern_constrains_values():
    model = create_pydantic_model_from_schema(
        {"code": {"type": "string", "pattern": "^[A-Z]+$", "maxLength": 5}}
    )

    assert model(code="ABC").code == "ABC"
    with pytest.raises(ValidationError):
        model(code="abc")
