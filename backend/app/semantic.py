import re
import uuid
from typing import Any

from .database import connection, rows_as_dicts, utcnow

BASE_TABLE = "fact_test_results"
VIEW_NAME = "vw_laboratory_analysis"
ANALYTICAL_TABLES = {
    "fact_test_results",
    "dim_vendor_contracts",
    "dim_project_budgets",
}
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

DEFAULT_RULES = [
    {
        "name": "Tests to vendor contracts",
        "left_table": BASE_TABLE,
        "left_field": "vendor",
        "right_table": "dim_vendor_contracts",
        "right_field": "vendor",
        "join_type": "LEFT",
        "relationship": "MANY_TO_ONE",
    },
    {
        "name": "Tests to project budgets",
        "left_table": BASE_TABLE,
        "left_field": "project",
        "right_table": "dim_project_budgets",
        "right_field": "project",
        "join_type": "LEFT",
        "relationship": "MANY_TO_ONE",
    },
]


class SemanticRuleError(ValueError):
    pass


def initialize_semantic_layer() -> None:
    with connection() as conn:
        count = conn.execute("SELECT count(*) FROM join_rules").fetchone()[0]
        if not count:
            _replace_rules(conn, DEFAULT_RULES)
        _publish_view(conn)


def get_semantic_layer() -> dict[str, Any]:
    with connection() as conn:
        rules = rows_as_dicts(conn.execute("SELECT * FROM join_rules ORDER BY created_at"))
        tables = [
            {"name": table, "columns": _columns(conn, table)}
            for table in sorted(ANALYTICAL_TABLES)
        ]
        preview = rows_as_dicts(conn.execute(f'SELECT * FROM "{VIEW_NAME}" LIMIT 10'))
        column_count = len(_columns(conn, VIEW_NAME))
    return {
        "base_table": BASE_TABLE,
        "view_name": VIEW_NAME,
        "tables": tables,
        "rules": rules,
        "view": {
            "status": "PUBLISHED",
            "column_count": column_count,
            "preview": preview,
        },
    }


def semantic_prompt_context() -> str:
    with connection() as conn:
        columns = conn.execute(f"DESCRIBE {_quote(VIEW_NAME)}").fetchall()
        rules = rows_as_dicts(
            conn.execute("SELECT * FROM join_rules WHERE status = 'PUBLISHED' ORDER BY created_at")
        )
    schema = ",\n  ".join(f"{row[0]} {row[1]}" for row in columns)
    relationships = "\n".join(
        f'- {rule["left_table"]}.{rule["left_field"]} = '
        f'{rule["right_table"]}.{rule["right_field"]} '
        f'({rule["join_type"]}, {rule["relationship"]})'
        for rule in rules
    )
    return (
        "Current published semantic layer (authoritative):\n"
        f"{VIEW_NAME}(\n  {schema}\n)\n"
        "Published relationships:\n"
        f"{relationships or '- No published relationships'}"
    )


def replace_semantic_rules(rules: list[dict[str, str]]) -> dict[str, Any]:
    if not rules:
        raise SemanticRuleError("At least one relationship is required")
    with connection() as conn:
        conn.execute("BEGIN")
        try:
            _replace_rules(conn, rules)
            _publish_view(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_semantic_layer()


def _replace_rules(conn, rules: list[dict[str, str]]) -> None:
    validated = []
    right_tables = set()
    for rule in rules:
        normalized = _normalize_rule(rule)
        if normalized["right_table"] in right_tables:
            raise SemanticRuleError("Each dimension table can only be joined once")
        right_tables.add(normalized["right_table"])
        matched_pct, right_unique, left_unique = _validate_rule(conn, normalized)
        if normalized["relationship"] in {"MANY_TO_ONE", "ONE_TO_ONE"} and not right_unique:
            raise SemanticRuleError(
                f'{normalized["right_table"]}.{normalized["right_field"]} must be unique '
                f'for {normalized["relationship"]}'
            )
        if normalized["relationship"] == "ONE_TO_ONE" and not left_unique:
            raise SemanticRuleError(
                f'{normalized["left_table"]}.{normalized["left_field"]} must be unique '
                "for ONE_TO_ONE"
            )
        validated.append((normalized, matched_pct, right_unique))

    now = utcnow()
    conn.execute("DELETE FROM join_rules")
    for rule, matched_pct, right_unique in validated:
        rule_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "join:" + ":".join(
                    [
                        rule["left_table"], rule["left_field"],
                        rule["right_table"], rule["right_field"],
                    ]
                ),
            )
        )
        conn.execute(
            "INSERT INTO join_rules VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PUBLISHED', ?, ?, ?, ?, ?)",
            [
                rule_id, rule["name"], rule["left_table"], rule["left_field"],
                rule["right_table"], rule["right_field"], rule["join_type"],
                rule["relationship"], VIEW_NAME, matched_pct, right_unique, now, now,
            ],
        )


def _normalize_rule(rule: dict[str, str]) -> dict[str, str]:
    normalized = {key: str(value).strip() for key, value in rule.items()}
    required = {
        "name", "left_table", "left_field", "right_table", "right_field",
        "join_type", "relationship",
    }
    if not required.issubset(normalized) or any(not normalized[key] for key in required):
        raise SemanticRuleError("Relationship fields cannot be empty")
    if normalized["left_table"] != BASE_TABLE:
        raise SemanticRuleError(f"The left table must be {BASE_TABLE}")
    if normalized["right_table"] not in ANALYTICAL_TABLES - {BASE_TABLE}:
        raise SemanticRuleError("The right table is not available for joins")
    if normalized["join_type"] not in {"LEFT", "INNER"}:
        raise SemanticRuleError("Only LEFT and INNER joins are supported")
    if normalized["relationship"] not in {"MANY_TO_ONE", "ONE_TO_ONE"}:
        raise SemanticRuleError("Only many-to-one and one-to-one relationships are supported")
    for key in ("left_table", "left_field", "right_table", "right_field"):
        if not IDENTIFIER.fullmatch(normalized[key]):
            raise SemanticRuleError(f"Invalid identifier: {normalized[key]}")
    return normalized


def _validate_rule(conn, rule: dict[str, str]) -> tuple[float, bool, bool]:
    left_columns = _columns(conn, rule["left_table"])
    right_columns = _columns(conn, rule["right_table"])
    if rule["left_field"] not in left_columns or rule["right_field"] not in right_columns:
        raise SemanticRuleError("A selected join field does not exist")
    left_table = _quote(rule["left_table"])
    left_field = _quote(rule["left_field"])
    right_table = _quote(rule["right_table"])
    right_field = _quote(rule["right_field"])
    total, matched = conn.execute(
        f"SELECT count(*), count(r.{right_field}) FROM {left_table} l "
        f"LEFT JOIN {right_table} r ON l.{left_field} = r.{right_field}"
    ).fetchone()
    count_value, distinct_value = conn.execute(
        f"SELECT count({right_field}), count(DISTINCT {right_field}) FROM {right_table}"
    ).fetchone()
    left_count, left_distinct = conn.execute(
        f"SELECT count({left_field}), count(DISTINCT {left_field}) FROM {left_table}"
    ).fetchone()
    matched_pct = round(100.0 * matched / total, 2) if total else 100.0
    return matched_pct, count_value == distinct_value, left_count == left_distinct


def _publish_view(conn) -> None:
    rules = rows_as_dicts(
        conn.execute("SELECT * FROM join_rules WHERE status = 'PUBLISHED' ORDER BY created_at")
    )
    selected = ["base.*"]
    joins = []
    used_columns = set(_columns(conn, BASE_TABLE))
    for index, rule in enumerate(rules):
        alias = f"dim_{index}"
        for column in _columns(conn, rule["right_table"]):
            if column == rule["right_field"]:
                continue
            output_name = column if column not in used_columns else f'{rule["right_table"]}_{column}'
            selected.append(f'{alias}.{_quote(column)} AS {_quote(output_name)}')
            used_columns.add(output_name)
        joins.append(
            f'{rule["join_type"]} JOIN {_quote(rule["right_table"])} {alias} '
            f'ON base.{_quote(rule["left_field"])} = {alias}.{_quote(rule["right_field"])}'
        )
    sql = (
        f'CREATE OR REPLACE VIEW {_quote(VIEW_NAME)} AS SELECT {", ".join(selected)} '
        f'FROM {_quote(BASE_TABLE)} base {" ".join(joins)}'
    )
    conn.execute(sql)


def _columns(conn, table: str) -> list[str]:
    if table not in ANALYTICAL_TABLES | {VIEW_NAME}:
        raise SemanticRuleError(f"Unknown analytical table: {table}")
    return [str(row[0]) for row in conn.execute(f"DESCRIBE {_quote(table)}").fetchall()]


def _quote(identifier: str) -> str:
    if not IDENTIFIER.fullmatch(identifier):
        raise SemanticRuleError(f"Invalid identifier: {identifier}")
    return f'"{identifier}"'
