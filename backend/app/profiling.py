from typing import Any

import polars as pl


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def profile_frame(frame: pl.DataFrame) -> dict[str, Any]:
    columns = []
    for name in frame.columns:
        series = frame.get_column(name)
        non_null = series.drop_nulls()
        samples = [_json_value(value) for value in non_null.head(3).to_list()]
        warnings = []
        null_rate = round(series.null_count() / max(frame.height, 1), 4)
        if null_rate > 0.2:
            warnings.append("High null rate")
        if non_null.n_unique() == 1 and frame.height > 1:
            warnings.append("Constant value")
        columns.append(
            {
                "name": name,
                "inferred_type": str(series.dtype),
                "null_rate": null_rate,
                "distinct_count": non_null.n_unique(),
                "sample_values": samples,
                "warnings": warnings,
            }
        )
    return {
        "row_count": frame.height,
        "column_count": frame.width,
        "columns": columns,
        "warnings": [],
    }


def preview_frame(frame: pl.DataFrame, limit: int = 20) -> list[dict[str, Any]]:
    return [
        {key: _json_value(value) for key, value in row.items()}
        for row in frame.head(limit).to_dicts()
    ]


def profile_text(content: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    preview = [{"line_number": index + 1, "content": line} for index, line in enumerate(lines[:20])]
    profile = {
        "row_count": max(1, len(lines)),
        "column_count": 2,
        "columns": [
            {"name": "line_number", "inferred_type": "Int64", "null_rate": 0, "distinct_count": len(lines), "sample_values": [1], "warnings": []},
            {"name": "content", "inferred_type": "String", "null_rate": 0, "distinct_count": len(set(lines)), "sample_values": lines[:3], "warnings": ["Unstructured text requires field extraction"]},
        ],
        "warnings": ["Profile describes source lines before semantic extraction"],
    }
    return profile, preview or [{"line_number": 1, "content": content}]
