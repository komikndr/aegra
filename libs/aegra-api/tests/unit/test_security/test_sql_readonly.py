import pytest
from sqlalchemy import create_engine, text

from aegra_api.security.sql_readonly import UnsafeSqlError, execute_read_only_sql, validate_read_only_sql


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "WITH values_cte AS (SELECT 1 AS value) SELECT value FROM values_cte",
        "SELECT 1 UNION SELECT 2",
        "DESCRIBE example_table",
        "EXPLAIN SELECT * FROM example_table",
    ],
)
def test_read_queries_are_allowed(query: str) -> None:
    validate_read_only_sql(query, "postgresql")


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1; SELECT 2",
        "WITH removed AS (DELETE FROM records RETURNING *) SELECT * FROM removed",
        "SELECT * INTO copied_records FROM records",
        "SELECT * FROM records FOR UPDATE",
        "PRAGMA writable_schema=ON",
        "ATTACH DATABASE '/tmp/other.db' AS other",
        "EXPLAIN ANALYZE DELETE FROM records",
        "SELECT pg_sleep(30)",
        "SELECT set_config('role', 'admin', false)",
    ],
)
def test_mutating_or_side_effecting_queries_are_rejected(query: str) -> None:
    with pytest.raises(UnsafeSqlError):
        validate_read_only_sql(query, "postgresql")


def test_unsupported_dialect_fails_closed() -> None:
    with pytest.raises(UnsafeSqlError, match="Unsupported SQL dialect"):
        validate_read_only_sql("SELECT 1", "mysql")


def test_sqlite_execution_is_bounded_and_rolled_back() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE records (value INTEGER)"))
        connection.execute(text("INSERT INTO records VALUES (1), (2), (3)"))

    columns, rows = execute_read_only_sql(engine, "SELECT value FROM records ORDER BY value", max_rows=2)

    assert columns == ["value"]
    assert rows == [{"value": 1}, {"value": 2}]


def test_sqlite_mutation_is_rejected_before_execution() -> None:
    engine = create_engine("sqlite:///:memory:")

    with pytest.raises(UnsafeSqlError):
        execute_read_only_sql(engine, "CREATE TABLE stolen (value INTEGER)", max_rows=10)
