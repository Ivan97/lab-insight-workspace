"""The canonical schema registry.

Before this existed the schema was written down in three places: the CREATE
TABLE statements, a hardcoded array in the React app, and a table listing
inside the planner prompt. They had already drifted -- the mapping dropdown
offered `sample_id`, `cost_amount` and `lab_vendor`, none of which exist, while
omitting six columns that do.

The registry is now the single description of what a canonical field means. It
is seeded from the physical tables, so it cannot invent a column, and it is
stored in DuckDB so it can be edited at runtime rather than only in source.

A field carries more than a name and a type. `description` is what the
Text-to-SQL prompt was missing, and `result_format` was previously re-decided
by the model on every single question even though it is a property of the
field.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .database import connection, rows_as_dicts, utcnow


class EntityRole(StrEnum):
    FACT = "FACT"
    DIMENSION = "DIMENSION"


class DataType(StrEnum):
    TEXT = "TEXT"
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    DATE = "DATE"
    BOOLEAN = "BOOLEAN"


class ResultFormat(StrEnum):
    """How a value is rendered. Mirrors the formats the A2UI table understands."""

    TEXT = "TEXT"
    INTEGER = "INTEGER"
    DECIMAL_2 = "DECIMAL_2"
    CURRENCY_USD = "CURRENCY_USD"
    PERCENT_2 = "PERCENT_2"
    DATE = "DATE"
    MONTH = "MONTH"
    DATETIME = "DATETIME"


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class FieldSeed:
    description: str
    data_type: DataType
    result_format: ResultFormat
    unit: str | None = None
    enum_values: tuple[str, ...] = ()
    is_key: bool = False


ENTITY_SEED: dict[str, tuple[EntityRole, str]] = {
    "fact_test_results": (EntityRole.FACT, "One laboratory test, the grain of all analysis."),
    "dim_vendor_contracts": (EntityRole.DIMENSION, "Commercial terms agreed with each vendor."),
    "dim_project_budgets": (EntityRole.DIMENSION, "Approved budget and ownership per project."),
    "dim_material_standards": (EntityRole.DIMENSION, "Quality and cost targets per material."),
}

# Descriptions are the point: they are what the planner prompt never had.
FIELD_SEED: dict[str, FieldSeed] = {
    "test_result_id": FieldSeed("Unique identifier of the test result.", DataType.TEXT, ResultFormat.TEXT, is_key=True),
    "vendor": FieldSeed("Laboratory that performed the test.", DataType.TEXT, ResultFormat.TEXT),
    "project": FieldSeed("Project the test was booked against.", DataType.TEXT, ResultFormat.TEXT),
    "material": FieldSeed("Material under test.", DataType.TEXT, ResultFormat.TEXT),
    "test_name": FieldSeed("Name of the test procedure.", DataType.TEXT, ResultFormat.TEXT),
    "submitted_date": FieldSeed("Date the sample was submitted.", DataType.DATE, ResultFormat.DATE),
    "completed_date": FieldSeed("Date the result was reported.", DataType.DATE, ResultFormat.DATE),
    "status": FieldSeed("Workflow state of the test.", DataType.TEXT, ResultFormat.TEXT),
    "result": FieldSeed("Outcome of the test.", DataType.TEXT, ResultFormat.TEXT, enum_values=("PASS", "FAIL")),
    "cost_usd": FieldSeed("Cost charged for the test.", DataType.DECIMAL, ResultFormat.CURRENCY_USD, unit="USD"),
    "turnaround_days": FieldSeed("Days between submission and completion.", DataType.INTEGER, ResultFormat.INTEGER, unit="days"),
    "contract_tier": FieldSeed("Commercial tier of the vendor contract.", DataType.TEXT, ResultFormat.TEXT),
    "region": FieldSeed("Region the vendor operates in.", DataType.TEXT, ResultFormat.TEXT),
    "contracted_cost_usd": FieldSeed("Cost per test agreed in the contract.", DataType.DECIMAL, ResultFormat.CURRENCY_USD, unit="USD"),
    "sla_days": FieldSeed("Turnaround the vendor committed to.", DataType.INTEGER, ResultFormat.INTEGER, unit="days"),
    "quality_target_pct": FieldSeed("Contracted pass rate, stored as a 0-1 fraction.", DataType.DECIMAL, ResultFormat.DECIMAL_2),
    "effective_date": FieldSeed("Date the contract took effect.", DataType.DATE, ResultFormat.DATE),
    "owner": FieldSeed("Person accountable for the project.", DataType.TEXT, ResultFormat.TEXT),
    "priority": FieldSeed("Business priority of the project.", DataType.TEXT, ResultFormat.TEXT),
    "approved_budget_usd": FieldSeed("Budget approved for the project.", DataType.DECIMAL, ResultFormat.CURRENCY_USD, unit="USD"),
    "start_date": FieldSeed("Project start date.", DataType.DATE, ResultFormat.DATE),
    "end_date": FieldSeed("Project end date.", DataType.DATE, ResultFormat.DATE),
    "material_family": FieldSeed("Broader family the material belongs to.", DataType.TEXT, ResultFormat.TEXT),
    "target_failure_pct": FieldSeed("Acceptable failure rate, on a 0-100 scale.", DataType.DECIMAL, ResultFormat.PERCENT_2),
    "max_avg_cost_usd": FieldSeed("Ceiling for the average cost of this material.", DataType.DECIMAL, ResultFormat.CURRENCY_USD, unit="USD"),
    "risk_tier": FieldSeed("Risk classification of the material.", DataType.TEXT, ResultFormat.TEXT),
}

_DUCKDB_TYPES: dict[str, DataType] = {
    "VARCHAR": DataType.TEXT,
    "INTEGER": DataType.INTEGER,
    "BIGINT": DataType.INTEGER,
    "DOUBLE": DataType.DECIMAL,
    "DECIMAL": DataType.DECIMAL,
    "DATE": DataType.DATE,
    "BOOLEAN": DataType.BOOLEAN,
}


def init_registry_schema() -> None:
    with connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_entities (
                entity VARCHAR PRIMARY KEY,
                display_name VARCHAR NOT NULL,
                description VARCHAR NOT NULL,
                role VARCHAR NOT NULL,
                updated_at TIMESTAMP NOT NULL
            );
            CREATE TABLE IF NOT EXISTS canonical_fields (
                entity VARCHAR NOT NULL,
                field VARCHAR NOT NULL,
                display_name VARCHAR NOT NULL,
                description VARCHAR NOT NULL,
                data_type VARCHAR NOT NULL,
                result_format VARCHAR NOT NULL,
                unit VARCHAR,
                enum_values VARCHAR,
                is_key BOOLEAN NOT NULL,
                nullable BOOLEAN NOT NULL,
                position INTEGER NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                PRIMARY KEY (entity, field)
            );
            """
        )


def _humanize(name: str) -> str:
    return name.replace("_", " ").capitalize()


def initialize_registry() -> None:
    """Seed from the physical tables, so the registry cannot invent a column."""
    init_registry_schema()
    now = utcnow()
    with connection() as conn:
        for entity, (role, description) in ENTITY_SEED.items():
            existing = conn.execute(
                "SELECT 1 FROM canonical_entities WHERE entity = ?", [entity]
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO canonical_entities VALUES (?, ?, ?, ?, ?)",
                    [entity, _humanize(entity.removeprefix("fact_").removeprefix("dim_")),
                     description, role.value, now],
                )
            columns = conn.execute(f'DESCRIBE "{entity}"').fetchall()
            for position, row in enumerate(columns):
                field, duck_type, nullable = str(row[0]), str(row[1]), str(row[2]) != "NO"
                if conn.execute(
                    "SELECT 1 FROM canonical_fields WHERE entity = ? AND field = ?",
                    [entity, field],
                ).fetchone():
                    continue
                seed = FIELD_SEED.get(field)
                data_type = (
                    seed.data_type if seed
                    else _DUCKDB_TYPES.get(duck_type.split("(")[0], DataType.TEXT)
                )
                conn.execute(
                    "INSERT INTO canonical_fields VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        entity, field, _humanize(field),
                        seed.description if seed else "",
                        data_type.value,
                        (seed.result_format if seed else ResultFormat.TEXT).value,
                        seed.unit if seed else None,
                        json.dumps(list(seed.enum_values)) if seed and seed.enum_values else None,
                        bool(seed.is_key) if seed else False,
                        nullable, position, now,
                    ],
                )


def list_fields() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = rows_as_dicts(
            conn.execute(
                "SELECT * FROM canonical_fields ORDER BY entity, position"
            )
        )
    for row in rows:
        row["enum_values"] = json.loads(row["enum_values"]) if row["enum_values"] else []
    return rows


def get_registry() -> dict[str, Any]:
    """Entities with their fields, which is what the schema page renders."""
    with connection() as conn:
        entities = rows_as_dicts(
            conn.execute("SELECT * FROM canonical_entities ORDER BY role DESC, entity")
        )
    fields = list_fields()
    by_entity: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        by_entity.setdefault(field["entity"], []).append(field)
    return {
        "entities": [
            {**entity, "fields": by_entity.get(entity["entity"], [])} for entity in entities
        ],
        "field_count": len(fields),
    }


def canonical_field_names() -> list[str]:
    """Targets a source field may be mapped onto.

    The mapping dropdown reads this instead of a literal in the React app,
    which is how it came to offer fields that do not exist.
    """
    return sorted({field["field"] for field in list_fields()})


def update_field(entity: str, field: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Edit the meaning of a field. Identifiers and types are not editable here:
    they belong to the physical table, and letting them drift is the bug this
    registry exists to prevent."""
    editable = {"display_name", "description", "result_format", "unit"}
    unknown = set(patch) - editable
    if unknown:
        raise RegistryError(
            f"Not editable: {', '.join(sorted(unknown))}. Editable: {', '.join(sorted(editable))}"
        )
    if "result_format" in patch:
        try:
            ResultFormat(str(patch["result_format"]))
        except ValueError:
            allowed = ", ".join(member.value for member in ResultFormat)
            raise RegistryError(
                f"result_format={patch['result_format']!r} is not supported. Use one of: {allowed}"
            ) from None
    with connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM canonical_fields WHERE entity = ? AND field = ?", [entity, field]
        ).fetchone()
        if not exists:
            raise RegistryError(f"Unknown field {entity}.{field}")
        for key, value in patch.items():
            conn.execute(
                f"UPDATE canonical_fields SET {key} = ?, updated_at = ? "
                "WHERE entity = ? AND field = ?",
                [value, utcnow(), entity, field],
            )
        row = rows_as_dicts(
            conn.execute(
                "SELECT * FROM canonical_fields WHERE entity = ? AND field = ?", [entity, field]
            )
        )[0]
    row["enum_values"] = json.loads(row["enum_values"]) if row["enum_values"] else []
    return row


def registry_prompt_context() -> str:
    """Field meanings for the planner prompt.

    Names and types alone left the model guessing what a column meant, and made
    it re-decide formatting on every question.
    """
    lines: list[str] = []
    for entity in get_registry()["entities"]:
        lines.append(f'{entity["entity"]} -- {entity["description"]}')
        for field in entity["fields"]:
            parts = [f'  {field["field"]} {field["data_type"]}']
            if field["description"]:
                parts.append(field["description"])
            if field["unit"]:
                parts.append(f'unit={field["unit"]}')
            if field["enum_values"]:
                parts.append(f'values={"/".join(field["enum_values"])}')
            parts.append(f'render as {field["result_format"]}')
            lines.append(" | ".join(parts))
    return "Canonical field meanings (authoritative):\n" + "\n".join(lines)
