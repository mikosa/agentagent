import pytest

from app.codex_rpc import SchemaValidator


def test_generated_schema_rejects_incomplete_interrupt():
    schemas = SchemaValidator()
    schemas.validate_params("turn/interrupt", {"threadId": "thread", "turnId": "turn"})
    with pytest.raises(ValueError):
        schemas.validate_params("turn/interrupt", {"turnId": "turn"})
    with pytest.raises(ValueError):
        schemas.validate_response("thread/start", {"thread": {}})
