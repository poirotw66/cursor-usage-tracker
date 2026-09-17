"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from cursor_usage_tracker.accuracy import build_accuracy_report, render_accuracy_table
from cursor_usage_tracker.arguments import build_parser
from cursor_usage_tracker.config import Settings
from cursor_usage_tracker.cursor_db import (
    CursorDatabaseError,
    cursor_database_health,
    sync_cursor_database,
)
from cursor_usage_tracker.daily_reporting import build_daily_report, render_daily_table
from cursor_usage_tracker.domain import ReportWindow
from cursor_usage_tracker.hook_install import HookInstallError, install_hooks
from cursor_usage_tracker.hooks import (
    InvalidHookPayload,
    process_hook,
    safe_hook_response,
)
from cursor_usage_tracker.model_configuration import (
    configure_default_model,
    get_configured_model,
    render_model_presets,
)
from cursor_usage_tracker.model_presets import MODEL_PRESETS
from cursor_usage_tracker.reporting import build_report, render_json, render_text
from cursor_usage_tracker.storage import Storage
from cursor_usage_tracker.telemetry import (
    import_telemetry,
    rebuild_calibration_profiles,
)

MAX_HOOK_PAYLOAD_BYTES = 64 * 1024 * 1024


def main(arguments: list[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    parsed = parser.parse_args(arguments)
    settings = Settings.from_environment()
    storage = Storage(settings.database_path)
    if parsed.command == "hook":
        return _run_hook(storage)
    try:
        return _dispatch(parsed, settings, storage)
    except (
        CursorDatabaseError,
        HookInstallError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _dispatch(
    arguments: argparse.Namespace, settings: Settings, storage: Storage
) -> int:
    if arguments.command == "init":
        storage.initialize()
        print(f"Initialized {storage.path}")
    elif arguments.command == "install-hook":
        executable = _executable_path()
        backup = install_hooks(settings.hooks_path, executable)
        if backup is None:
            print(f"Hooks already installed in {settings.hooks_path}")
        else:
            print(f"Installed hooks in {settings.hooks_path}")
            print(f"Backup: {backup}")
    elif arguments.command == "sync":
        storage.initialize()
        result = sync_cursor_database(settings.cursor_database_path, storage)
        print(
            f"Resolved: {result.resolved_models}; "
            f"selected: {result.selected_models}; ignored: {result.ignored_models}"
        )
    elif arguments.command == "snapshot":
        storage.initialize()
        _add_snapshot(arguments, storage)
    elif arguments.command == "calibration":
        storage.initialize()
        _add_calibration(arguments, storage)
    elif arguments.command == "rate":
        storage.initialize()
        _add_rate(arguments, storage)
    elif arguments.command == "model":
        storage.initialize()
        _configure_model(arguments, storage)
    elif arguments.command == "telemetry":
        storage.initialize()
        _handle_telemetry(arguments, storage)
    elif arguments.command == "report":
        storage.initialize()
        report_window = _report_window(arguments)
        if arguments.daily and arguments.accuracy:
            raise ValueError("--daily and --accuracy cannot be combined")
        if arguments.accuracy:
            accuracy_report = build_accuracy_report(storage, report_window)
            print(
                render_json(accuracy_report)
                if arguments.json
                else render_accuracy_table(accuracy_report)
            )
        elif arguments.daily:
            daily_report = build_daily_report(storage, report_window)
            print(
                render_json(daily_report)
                if arguments.json
                else render_daily_table(daily_report)
            )
        else:
            report = build_report(storage, report_window)
            print(render_json(report) if arguments.json else render_text(report))
    elif arguments.command == "doctor":
        return _doctor(settings, storage)
    else:
        raise ValueError(f"Unsupported command: {arguments.command}")
    return 0


def _run_hook(storage: Storage) -> int:
    event_name: str | None = None
    try:
        raw_payload = sys.stdin.buffer.read(MAX_HOOK_PAYLOAD_BYTES + 1)
        if len(raw_payload) > MAX_HOOK_PAYLOAD_BYTES:
            raise InvalidHookPayload("Hook payload exceeds 64 MiB")
        payload = json.loads(raw_payload)
        if isinstance(payload, dict):
            raw_event_name = payload.get("hook_event_name")
            event_name = raw_event_name if isinstance(raw_event_name, str) else None
        response = process_hook(payload, storage)
    except (InvalidHookPayload, json.JSONDecodeError, OSError, sqlite3.Error) as error:
        print(f"cursor-usage hook warning: {error}", file=sys.stderr)
        response = safe_hook_response(event_name)
    print(json.dumps(response, separators=(",", ":")))
    return 0


def _add_snapshot(arguments: argparse.Namespace, storage: Storage) -> None:
    values = (
        arguments.total_events,
        arguments.events_with_token,
        arguments.events_without_token,
        arguments.known_input,
        arguments.known_output,
        arguments.known_cache_read,
        arguments.known_cache_write,
    )
    _require_nonnegative(values)
    if arguments.events_with_token + arguments.events_without_token != (
        arguments.total_events
    ):
        raise ValueError("with-token + without-token must equal total-events")
    captured_at = _parse_timestamp(arguments.captured_at)
    storage.add_dashboard_snapshot(
        captured_at=captured_at.isoformat(),
        total_events=arguments.total_events,
        events_with_token=arguments.events_with_token,
        events_without_token=arguments.events_without_token,
        known_input=arguments.known_input,
        known_output=arguments.known_output,
        known_cache_read=arguments.known_cache_read,
        known_cache_write=arguments.known_cache_write,
    )
    print(f"Recorded dashboard snapshot at {captured_at.isoformat()}")


def _add_calibration(arguments: argparse.Namespace, storage: Storage) -> None:
    values = (
        arguments.input_point,
        arguments.input_low,
        arguments.input_high,
        arguments.output_point,
        arguments.output_low,
        arguments.output_high,
        arguments.sample_size,
    )
    _require_nonnegative(values)
    _require_ordered_range(
        arguments.input_low, arguments.input_point, arguments.input_high, "input"
    )
    _require_ordered_range(
        arguments.output_low,
        arguments.output_point,
        arguments.output_high,
        "output",
    )
    if arguments.sample_size < 1:
        raise ValueError("sample-size must be at least 1")
    storage.add_calibration(
        model=arguments.model,
        input_point=arguments.input_point,
        input_low=arguments.input_low,
        input_high=arguments.input_high,
        output_point=arguments.output_point,
        output_low=arguments.output_low,
        output_high=arguments.output_high,
        sample_size=arguments.sample_size,
        updated_at=datetime.now(UTC).isoformat(),
    )
    print(f"Set calibration for {arguments.model}")


def _add_rate(arguments: argparse.Namespace, storage: Storage) -> None:
    values = [
        arguments.input_per_million,
        arguments.output_per_million,
    ]
    values.extend(
        value
        for value in (
            arguments.cache_read_per_million,
            arguments.cache_write_per_million,
        )
        if value is not None
    )
    if any(value < 0 for value in values):
        raise ValueError("Rates cannot be negative")
    effective_at = _parse_timestamp(arguments.effective_at)
    storage.add_rate(
        model=arguments.model,
        effective_at=effective_at.isoformat(),
        input_per_million=arguments.input_per_million,
        output_per_million=arguments.output_per_million,
        cache_read_per_million=arguments.cache_read_per_million,
        cache_write_per_million=arguments.cache_write_per_million,
    )
    print(f"Set rate for {arguments.model} effective {effective_at.isoformat()}")


def _configure_model(arguments: argparse.Namespace, storage: Storage) -> None:
    configured = get_configured_model(storage)
    configured_slug = configured.slug if configured is not None else None
    if arguments.action == "list":
        print(render_model_presets(configured_slug))
        return
    if arguments.action == "show":
        if configured is None:
            print("No default model configured.")
        else:
            print(render_model_presets(configured.slug))
        return
    if arguments.action == "set":
        if arguments.model is None:
            raise ValueError("model set requires a model slug")
        _save_model_choice(storage, arguments.model, arguments)
        return

    print(render_model_presets(configured_slug))
    choice = input("\nChoose model number or slug: ").strip()
    if choice.isdigit():
        index = int(choice) - 1
        if index < 0 or index >= len(MODEL_PRESETS):
            raise ValueError("Model number is out of range")
        choice = MODEL_PRESETS[index].slug
    _save_model_choice(storage, choice, arguments)


def _save_model_choice(
    storage: Storage, slug: str, arguments: argparse.Namespace
) -> None:
    scope_type = "global"
    scope_value = "*"
    if arguments.repo is not None:
        scope_type = "repository"
        scope_value = arguments.repo
    elif arguments.session is not None:
        scope_type = "session"
        scope_value = arguments.session
    effective_from = (
        _parse_timestamp(arguments.effective_from)
        if arguments.effective_from is not None
        else None
    )
    preset = configure_default_model(
        storage,
        slug,
        scope_type=scope_type,
        scope_value=scope_value,
        effective_from=effective_from,
    )
    print(f"Configured reference model: {preset.display_name}")
    print(f"Scope: {scope_type}={scope_value}; effective from now or supplied time")
    print("Note: reference only; this does not prove Cursor Auto routing or billing.")


def _handle_telemetry(arguments: argparse.Namespace, storage: Storage) -> None:
    if arguments.action == "import":
        if arguments.path is None:
            raise ValueError("telemetry import requires a CSV or JSONL path")
        result = import_telemetry(Path(arguments.path), storage)
        print(
            f"Imported {result.observations} observations; "
            f"rebuilt {result.profiles} calibration profiles"
        )
        return
    profiles = rebuild_calibration_profiles(storage)
    if not profiles:
        print("No calibration profiles. Import telemetry first.")
        return
    for profile in profiles:
        confidence = "medium" if profile.sample_size >= 30 else "low"
        print(
            f"{profile.model} effort={profile.effort} "
            f"tools={profile.tool_bucket} attachments={profile.attachment_bucket} "
            f"n={profile.sample_size} confidence={confidence} "
            f"input={profile.input_p10}/{profile.input_p50}/{profile.input_p90} "
            f"output={profile.output_p10}/{profile.output_p50}/{profile.output_p90}"
        )


def _doctor(settings: Settings, storage: Storage) -> int:
    failures = 0
    try:
        storage.initialize()
        storage.query("SELECT version FROM schema_version")
        print(f"Tracker database: OK ({storage.path})")
    except (OSError, sqlite3.Error) as error:
        print(f"Tracker database: FAIL ({error})")
        failures += 1
    cursor_ok, cursor_message = cursor_database_health(settings.cursor_database_path)
    print(f"Cursor database: {'OK' if cursor_ok else 'FAIL'} ({cursor_message})")
    failures += int(not cursor_ok)
    hook_status = (
        "present" if settings.hooks_path.exists() else "not installed/configured"
    )
    print(f"Cursor hooks: {hook_status} ({settings.hooks_path})")
    return int(failures > 0)


def _report_window(arguments: argparse.Namespace) -> ReportWindow:
    local_timezone = datetime.now().astimezone().tzinfo
    if local_timezone is None:
        local_timezone = UTC
    if arguments.start or arguments.end:
        if not arguments.start or not arguments.end:
            raise ValueError("--start and --end must be supplied together")
        start_date = date.fromisoformat(arguments.start)
        end_date = date.fromisoformat(arguments.end)
        if end_date <= start_date:
            raise ValueError("--end must be after --start")
        return ReportWindow(
            starts_at=datetime.combine(start_date, time.min, local_timezone),
            ends_at=datetime.combine(end_date, time.min, local_timezone),
        )
    report_date = date.fromisoformat(arguments.date) if arguments.date else date.today()
    starts_at = datetime.combine(report_date, time.min, local_timezone)
    return ReportWindow(starts_at=starts_at, ends_at=starts_at + timedelta(days=1))


def _parse_timestamp(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _require_nonnegative(values: tuple[int, ...]) -> None:
    if any(value < 0 for value in values):
        raise ValueError("Counts cannot be negative")


def _require_ordered_range(low: int, point: int, high: int, name: str) -> None:
    if not low <= point <= high:
        raise ValueError(f"{name} range must satisfy low <= point <= high")


def _executable_path() -> Path:
    executable = shutil.which("cursor-usage")
    if executable is None:
        raise HookInstallError(
            "cursor-usage is not installed on PATH; run `uv tool install .` first"
        )
    return Path(executable)


if __name__ == "__main__":
    raise SystemExit(main())
