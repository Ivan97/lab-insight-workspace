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


def initialize_demo(seed: int = 20260802) -> tuple[bool, int, list[str]]:
    with connection() as conn:
        count = conn.execute("SELECT count(*) FROM fact_test_results").fetchone()[0]
        if count:
            _ensure_demo_payloads(conn)
            batches = [
                row[0] for row in conn.execute("SELECT batch_id FROM ingestion_batches").fetchall()
            ]
            return False, count, batches

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
            fail_probability = (
                0.29 if material == "Polymer-A" and test_name == "Thermal Cycling" else 0.09
            )
            result = "FAIL" if rng.random() < fail_probability else "PASS"
            base_cost = {"BluePeak": 82, "Aster Labs": 106, "DeltaLab": 112}.get(vendor, 98)
            if vendor == "DeltaLab" and submitted >= date(2026, 6, 1):
                base_cost *= 1.24
            cost = round(max(35, rng.gauss(base_cost, 18)), 2)
            records.append(
                (
                    f"TR-{index + 1:05d}",
                    vendor,
                    rng.choice(PROJECTS),
                    material,
                    test_name,
                    submitted,
                    completed,
                    "COMPLETED",
                    result,
                    cost,
                    turnaround,
                )
            )
        conn.executemany(
            "INSERT INTO fact_test_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", records
        )

        now = utcnow()
        batches = []
        sources = [
            ("CSV", "vendor-a-results.csv", "Aster Labs", "READY", 350, 98),
            ("XLSX", "bluepeak-q2.xlsx", "BluePeak", "NEEDS_REVIEW", 300, 84),
            ("TEXT", "slack-lab-updates.txt", None, "READY", 42, 91),
        ]
        for source_type, source_name, vendor, status, record_count, score in sources:
            batch_id = str(uuid.uuid4())
            batches.append(batch_id)
            conn.execute(
                "INSERT INTO ingestion_batches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    batch_id,
                    source_type,
                    source_name,
                    vendor,
                    status,
                    record_count,
                    score,
                    "Ready for analysis" if status == "READY" else "Review field mapping",
                    now,
                    now,
                ],
            )
            mappings = default_mappings(batch_id, source_type)
            conn.execute(
                "INSERT INTO field_mappings VALUES (?, 1, ?)", [batch_id, json_dumps(mappings)]
            )
        _ensure_demo_payloads(conn)
        return True, len(records), batches


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


def default_mappings(batch_id: str, source_type: str) -> list[dict]:
    if source_type == "XLSX":
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
