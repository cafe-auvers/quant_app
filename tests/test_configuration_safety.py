import pytest

from src.utils.db_loader import get_mysql_connection_url, validate_mysql_identifier


@pytest.mark.parametrize(
    "value",
    ["quant_app", "db2", "_local", "A" * 64],
)
def test_validate_mysql_identifier_accepts_allowlisted_names(value):
    assert validate_mysql_identifier(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "quant-app", "quant app", "db` ; DROP DATABASE mysql; --", "A" * 65],
)
def test_validate_mysql_identifier_rejects_unsafe_names(value):
    with pytest.raises(ValueError, match="Invalid database name"):
        validate_mysql_identifier(value)


def test_connection_url_rejects_unsafe_database_before_engine_creation():
    with pytest.raises(ValueError, match="Invalid database name"):
        get_mysql_connection_url("bad`name")
