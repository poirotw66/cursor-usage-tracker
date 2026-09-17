"""Import known token telemetry and derive conditional calibration profiles."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cursor_usage_tracker.domain import CalibrationProfile, TelemetryObservation
from cursor_usage_tracker.storage import Storage
from cursor_usage_tracker.telemetry_storage import TelemetryRepository


@dataclass(frozen=True, slots=True)
class TelemetryImportResult:
    """Summary of one telemetry import and profile rebuild."""

    observations: int
    profiles: int


def import_telemetry(path: Path, storage: Storage) -> TelemetryImportResult:
    """Import JSONL or CSV telemetry without retaining arbitrary fields."""
    resolved_path = path.expanduser().resolve(strict=True)
    imported_at = datetime.now(UTC).isoformat()
    if resolved_path.suffix.casefold() == ".csv":
        records = _read_csv(resolved_path)
    elif resolved_path.suffix.casefold() in {".jsonl", ".ndjson"}:
        records = _read_jsonl(resolved_path)
    else:
        raise ValueError("Telemetry file must use .csv, .jsonl, or .ndjson")
    observations = [
        _parse_observation(record, imported_at=imported_at) for record in records
    ]
    if not observations:
        raise ValueError("Telemetry file contains no observations")
    TelemetryRepository(storage).import_observations(observations)
    profiles = rebuild_calibration_profiles(storage)
    return TelemetryImportResult(
        observations=len(observations),
        profiles=len(profiles),
    )


def rebuild_calibration_profiles(storage: Storage) -> list[CalibrationProfile]:
    """Recompute p10/p50/p90 profiles from all imported observations."""
    grouped: dict[
        tuple[str, str, str, str],
        list[tuple[int, int, int, int]],
    ] = defaultdict(list)
    for row in storage.query("SELECT * FROM telemetry_observations"):
        key = (
            str(row["model"]),
            str(row["effort"]),
            _tool_bucket(int(row["tool_count"])),
            _attachment_bucket(int(row["attachment_count"])),
        )
        grouped[key].append(
            (
                int(row["input_tokens"]),
                int(row["output_tokens"]),
                int(row["cache_read_tokens"]),
                int(row["cache_write_tokens"]),
            )
        )
    updated_at = datetime.now(UTC).isoformat()
    profiles = [
        _profile(key, values, updated_at=updated_at)
        for key, values in sorted(grouped.items())
    ]
    TelemetryRepository(storage).replace_profiles(profiles)
    return profiles


def _read_csv(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8", newline="") as file_handle:
        return [dict(row) for row in csv.DictReader(file_handle)]


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as file_handle:
        for line_number, line in enumerate(file_handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSON on line {line_number}") from error
            if not isinstance(value, dict) or not all(
                isinstance(key, str) for key in value
            ):
                raise ValueError(f"Line {line_number} must contain a JSON object")
            records.append(value)
    return records


def _parse_observation(
    record: dict[str, object], *, imported_at: str
) -> TelemetryObservation:
    observed_at = _timestamp(record, "timestamp")
    model = _required_text(record, "model")
    effort = _optional_text(record, "effort") or "unknown"
    request_id = _optional_text(record, "request_id")
    numeric_values = {
        key: _nonnegative_integer(record, key, default=0)
        for key in (
            "tool_count",
            "attachment_count",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
    }
    if numeric_values["input_tokens"] + numeric_values["output_tokens"] == 0:
        raise ValueError("Telemetry observation must contain input or output tokens")
    observation_id = _optional_text(record, "observation_id") or _observation_id(
        request_id=request_id,
        observed_at=observed_at,
        model=model,
        effort=effort,
        numeric_values=numeric_values,
    )
    return TelemetryObservation(
        observation_id=observation_id,
        request_id=request_id,
        observed_at=observed_at,
        model=model,
        effort=effort,
        tool_count=numeric_values["tool_count"],
        attachment_count=numeric_values["attachment_count"],
        input_tokens=numeric_values["input_tokens"],
        output_tokens=numeric_values["output_tokens"],
        cache_read_tokens=numeric_values["cache_read_tokens"],
        cache_write_tokens=numeric_values["cache_write_tokens"],
        imported_at=imported_at,
    )


def _profile(
    key: tuple[str, str, str, str],
    values: list[tuple[int, int, int, int]],
    *,
    updated_at: str,
) -> CalibrationProfile:
    inputs, outputs, cache_reads, cache_writes = zip(*values, strict=True)
    return CalibrationProfile(
        model=key[0],
        effort=key[1],
        tool_bucket=key[2],
        attachment_bucket=key[3],
        sample_size=len(values),
        input_p10=_percentile(inputs, 0.1),
        input_p50=_percentile(inputs, 0.5),
        input_p90=_percentile(inputs, 0.9),
        output_p10=_percentile(outputs, 0.1),
        output_p50=_percentile(outputs, 0.5),
        output_p90=_percentile(outputs, 0.9),
        cache_read_p50=_percentile(cache_reads, 0.5),
        cache_write_p50=_percentile(cache_writes, 0.5),
        updated_at=updated_at,
    )


def _percentile(values: tuple[int, ...], quantile: float) -> int:
    ordered = sorted(values)
    index = round((len(ordered) - 1) * quantile)
    return ordered[index]


def _tool_bucket(count: int) -> str:
    if count == 0:
        return "0"
    return "1-5" if count <= 5 else "6+"


def _attachment_bucket(count: int) -> str:
    if count == 0:
        return "0"
    return "1-3" if count <= 3 else "4+"


def _timestamp(record: dict[str, object], key: str) -> str:
    raw_value = _required_text(record, key)
    parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{key} must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _required_text(record: dict[str, object], key: str) -> str:
    value = _optional_text(record, key)
    if value is None:
        raise ValueError(f"Missing required text field: {key}")
    return value


def _optional_text(record: dict[str, object], key: str) -> str | None:
    value = record.get(key)
    return value if isinstance(value, str) and value else None


def _nonnegative_integer(record: dict[str, object], key: str, *, default: int) -> int:
    value = record.get(key, default)
    try:
        parsed = int(value) if isinstance(value, (int, str)) else default
    except ValueError as error:
        raise ValueError(f"{key} must be an integer") from error
    if parsed < 0:
        raise ValueError(f"{key} cannot be negative")
    return parsed


def _observation_id(
    *,
    request_id: str | None,
    observed_at: str,
    model: str,
    effort: str,
    numeric_values: dict[str, int],
) -> str:
    stable = json.dumps(
        {
            "request_id": request_id,
            "observed_at": observed_at,
            "model": model,
            "effort": effort,
            **numeric_values,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(stable.encode()).hexdigest()
