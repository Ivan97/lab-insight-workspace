import json
import random
import uuid
from datetime import date, timedelta

from .database import connection, json_dumps, utcnow

VENDORS = ["Aster Labs", "BluePeak", "CoreTest", "DeltaLab", "Element Six", "Futura"]
PROJECTS = [f"Project {letter}" for letter in "ABCDEFGHIJ"]
MATERIALS = ["Alloy-X", "Polymer-A", "Ceramic-C", "Composite-Z", "Copper-M"]
TESTS = [
    "Tensile Strength",
    "Thermal Cycling",
    "Humidity Exposure",
    "Salt Spray",
    "Impact Resistance",
    "Chemical Compatibility",
]

DEMO_SOURCES = [
    ("CSV", "vendor-a-results.csv", "Aster Labs", "READY", 350, 98),
    ("XLSX", "bluepeak-q2.xlsx", "BluePeak", "NEEDS_REVIEW", 300, 84),
    ("TEXT", "slack-lab-updates.txt", None, "READY", 42, 91),
    ("CSV", "vendor-contracts.csv", "Contract reference", "READY", 6, 99),
    ("XLSX", "project-budgets.xlsx", "Portfolio planning", "READY", 10, 97),
]

VENDOR_CONTRACTS = [
    ("Aster Labs", "Strategic", "North America", 102.0, 8, 0.92, date(2026, 1, 1)),
    ("BluePeak", "Preferred", "Asia Pacific", 85.0, 10, 0.90, date(2026, 1, 1)),
    ("CoreTest", "Preferred", "Europe", 96.0, 7, 0.91, date(2026, 1, 1)),
    ("DeltaLab", "Standard", "North America", 105.0, 8, 0.89, date(2026, 1, 1)),
    ("Element Six", "Standard", "Europe", 101.0, 7, 0.90, date(2026, 1, 1)),
    ("Futura", "Preferred", "Asia Pacific", 94.0, 7, 0.91, date(2026, 1, 1)),
]

PROJECT_BUDGETS = [
    (
        project,
        ["Maya Chen", "Noah Williams", "Sofia Patel", "Leo Martin", "Emma Garcia"][index % 5],
        ["High", "Medium", "Medium", "Low", "High"][index % 5],
        8500.0 + index * 1750.0,
        date(2026, 1 + index % 3, 1),
        [date(2026, 8, 31), date(2026, 9, 30), date(2026, 10, 31)][index % 3],
    )
    for index, project in enumerate(PROJECTS)
]


def initialize_demo(seed: int = 20260802) -> tuple[bool, int, list[str]]:
    with connection() as conn:
        count = conn.execute("SELECT count(*) FROM fact_test_results").fetchone()[0]
        initialized = not count
        if initialized:
            rng = random.Random(seed)
            start = date(2026, 2, 1)
            records = []
            for index in range(1_000):
                vendor = rng.choice(VENDORS)
                submitted = start + timedelta(days=rng.randrange(181))
                base_turnaround = {"BluePeak": 12, "Aster Labs": 7, "DeltaLab": 8}.get(vendor, 6)
                turnaround = max(2, round(rng.gauss(base_turnaround, 2.2)))
                completed = submitted + timedelta(days=turnaround)
                material = rng.choice(MATERIALS)
                test_name = rng.choice(TESTS)
                fail_probability = 0.29 if material == "Polymer-A" and test_name == "Thermal Cycling" else 0.09
                result = "FAIL" if rng.random() < fail_probability else "PASS"
                base_cost = {"BluePeak": 82, "Aster Labs": 106, "DeltaLab": 112}.get(vendor, 98)
                if vendor == "DeltaLab" and submitted >= date(2026, 6, 1):
                    base_cost *= 1.24
                records.append(
                    (
                        f"TR-{index + 1:05d}", vendor, rng.choice(PROJECTS), material, test_name,
                        submitted, completed, "COMPLETED", result,
                        round(max(35, rng.gauss(base_cost, 18)), 2), turnaround,
                    )
                )
            conn.executemany(
                "INSERT INTO fact_test_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", records
            )
            count = len(records)

        _ensure_reference_tables(conn)
        batches = _ensure_demo_batches(conn)
        _ensure_demo_payloads(conn)
        return initialized, count, batches


def _ensure_reference_tables(conn) -> None:
    for row in VENDOR_CONTRACTS:
        conn.execute(
            "INSERT OR REPLACE INTO dim_vendor_contracts VALUES (?, ?, ?, ?, ?, ?, ?)", row
        )
    for row in PROJECT_BUDGETS:
        conn.execute(
            "INSERT OR REPLACE INTO dim_project_budgets VALUES (?, ?, ?, ?, ?, ?)", row
        )


def _ensure_demo_batches(conn) -> list[str]:
    now = utcnow()
    for source_type, source_name, vendor, status, record_count, score in DEMO_SOURCES:
        existing = conn.execute(
            "SELECT batch_id FROM ingestion_batches WHERE source_name = ?", [source_name]
        ).fetchone()
        if existing:
            continue
        batch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mini-hackathon:{source_name}"))
        conn.execute(
            "INSERT INTO ingestion_batches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                batch_id, source_type, source_name, vendor, status, record_count, score,
                "Ready for analysis" if status == "READY" else "Review field mapping", now, now,
            ],
        )
        conn.execute(
            "INSERT INTO field_mappings VALUES (?, 1, ?)",
            [batch_id, json_dumps(default_mappings(batch_id, source_type, source_name))],
        )
    return [row[0] for row in conn.execute("SELECT batch_id FROM ingestion_batches").fetchall()]


def _ensure_demo_payloads(conn) -> None:
    rows = conn.execute(
        """
        SELECT b.batch_id, b.record_count, m.payload
        FROM ingestion_batches b
        JOIN field_mappings m USING (batch_id)
        LEFT JOIN ingestion_payloads p USING (batch_id)
        WHERE p.batch_id IS NULL
        """
    ).fetchall()
    for batch_id, record_count, payload in rows:
        mappings = json.loads(payload)
        columns = [
            {
                "name": item["source_field"],
                "inferred_type": "String",
                "null_rate": 0.0,
                "distinct_count": min(record_count, 48),
                "sample_values": item.get("sample_before", []),
                "warnings": item.get("warnings", []),
            }
            for item in mappings
        ]
        preview = [
            {item["source_field"]: (item.get("sample_before") or ["Example value"])[0] for item in mappings}
        ]
        profile = {
            "row_count": record_count,
            "column_count": len(columns),
            "columns": columns,
            "warnings": [],
        }
        conn.execute(
            "INSERT INTO ingestion_payloads VALUES (?, ?, ?)",
            [batch_id, json_dumps(profile), json_dumps(preview)],
        )


def default_mappings(batch_id: str, source_type: str, source_name: str = "") -> list[dict]:
    if source_name == "vendor-contracts.csv":
        fields = [
            ("vendor", "vendor", 0.99, "TRIM", []),
            ("contract_tier", "contract_tier", 0.98, "MAP_ENUM", []),
            ("contracted_cost_usd", "contracted_cost_usd", 0.99, "PARSE_MONEY", []),
            ("sla_days", "sla_days", 0.99, "PARSE_INTEGER", []),
            ("quality_target_pct", "quality_target_pct", 0.99, "PARSE_PERCENT", []),
        ]
    elif source_name == "project-budgets.xlsx":
        fields = [
            ("project", "project", 0.99, "TRIM", []),
            ("owner", "owner", 0.98, "TRIM", []),
            ("priority", "priority", 0.98, "MAP_ENUM", []),
            ("approved_budget_usd", "approved_budget_usd", 0.99, "PARSE_MONEY", []),
            ("start_date", "start_date", 0.99, "PARSE_DATE", []),
            ("end_date", "end_date", 0.99, "PARSE_DATE", []),
        ]
    elif source_type == "XLSX":
        fields = [
            ("specimen_id", "sample_id", 0.96, "IDENTITY", []),
            ("analysis_name", "test_name", 0.92, "TRIM", []),
            ("lab_status", "result", 0.72, "MAP_ENUM", ["Confirm whether NG means FAIL"]),
            ("invoice_amt", "cost_amount", 0.89, "PARSE_MONEY", []),
            ("finish_dt", "completed_date", 0.94, "PARSE_DATE", []),
        ]
    else:
        fields = [
            ("sample_no", "sample_id", 0.98, "TRIM", []),
            ("test_item", "test_name", 0.97, "TRIM", []),
            ("result", "result", 0.99, "MAP_ENUM", []),
            ("cost", "cost_amount", 0.95, "PARSE_MONEY", []),
        ]
    return [
        {
            "source_field": source,
            "target_field": target,
            "confidence": confidence,
            "transform": transform,
            "reason": "Matched by field semantics and representative values",
            "status": "SUGGESTED",
            "sample_before": ["Example value"],
            "sample_after": ["Normalized value"],
            "warnings": warnings,
        }
        for source, target, confidence, transform, warnings in fields
    ]
