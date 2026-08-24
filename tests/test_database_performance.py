from sqlalchemy import create_engine, inspect, text

from src.infrastructure.database.schema import (
    _ensure_price_history_indexes,
    _ensure_price_history_table,
)


def test_price_history_watermark_has_covering_interval_date_index():
    engine = create_engine("sqlite:///:memory:", future=True)

    _ensure_price_history_table(engine)
    assert _ensure_price_history_indexes(engine) is True
    assert _ensure_price_history_indexes(engine) is True

    indexes = {
        index["name"]: tuple(index["column_names"])
        for index in inspect(engine).get_indexes("price_history")
    }
    assert indexes["ix_price_history_interval_date"] == ("interval", "date")
    assert indexes["ix_price_history_symbol_interval_date"] == (
        "symbol",
        "interval",
        "date",
    )

    with engine.connect() as connection:
        plan = connection.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT MAX(date) FROM price_history "
                "WHERE interval = '1d'"
            )
        ).all()

    assert "ix_price_history_interval_date" in " ".join(
        str(value) for row in plan for value in row
    )

    with engine.connect() as connection:
        chart_plan = connection.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT * FROM price_history "
                "WHERE symbol = 'AAPL' AND interval = '1d' "
                "ORDER BY date DESC LIMIT 260"
            )
        ).all()

    assert "ix_price_history_symbol_interval_date" in " ".join(
        str(value) for row in chart_plan for value in row
    )


def test_existing_price_history_gets_index_during_schema_setup():
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE price_history ("
                "symbol VARCHAR(20) NOT NULL, date DATETIME NOT NULL, "
                "interval VARCHAR(10) NOT NULL, open FLOAT, high FLOAT, "
                "low FLOAT, close FLOAT, adj_close FLOAT, volume FLOAT, "
                "updated_at DATETIME NOT NULL, "
                "PRIMARY KEY (symbol, date, interval))"
            )
        )

    _ensure_price_history_table(engine)
    assert inspect(engine).get_indexes("price_history") == []

    assert _ensure_price_history_indexes(engine) is True
    assert {
        index["name"] for index in inspect(engine).get_indexes("price_history")
    } == {
        "ix_price_history_interval_date",
        "ix_price_history_symbol_interval_date",
    }
