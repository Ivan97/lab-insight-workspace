import re

from sqlglot import exp, parse
from sqlglot.errors import ParseError

ALLOWED_TABLES = {
    "fact_test_results",
    "dim_vendor_contracts",
    "dim_project_budgets",
    "dim_material_standards",
    "vw_laboratory_analysis",
}
DISALLOWED_TOKENS = re.compile(
    r"\b(attach|copy|create|delete|detach|drop|export|import|insert|install|load|pragma|replace|truncate|update)\b",
    re.IGNORECASE,
)
EXTERNAL_READERS = re.compile(
    r"\b(glob|query|query_table|read_blob|read_csv|read_json|read_parquet|read_text|parquet_scan|sqlite_scan|postgres_scan|httpfs)\s*\(",
    re.IGNORECASE,
)


class SQLGuardError(ValueError):
    """Raised when generated SQL is outside the read-only analytical policy."""


def guard_sql(sql: str) -> str:
    if DISALLOWED_TOKENS.search(sql) or EXTERNAL_READERS.search(sql):
        raise SQLGuardError("Only read-only analytical SQL is allowed")
    try:
        statements = [statement for statement in parse(sql, read="duckdb") if statement]
    except ParseError as exc:
        raise SQLGuardError("SQL could not be parsed") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise SQLGuardError("Exactly one SELECT query is required")

    statement = statements[0]
    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    tables = {table.name.lower() for table in statement.find_all(exp.Table)} - cte_names
    unknown = tables - ALLOWED_TABLES
    if unknown:
        raise SQLGuardError(f"Query references unapproved tables: {', '.join(sorted(unknown))}")
    if not tables:
        raise SQLGuardError("Query must reference the approved analytical view")
    if statement.args.get("limit") is None:
        statement = statement.limit(200)
    # Serialize the validated AST once and reuse this exact SQL for EXPLAIN,
    # execution, tool-call review, and the final disclosure panel.
    return statement.sql(dialect="duckdb", pretty=True)
