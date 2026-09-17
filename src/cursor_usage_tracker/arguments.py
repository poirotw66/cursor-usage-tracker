"""Command-line argument definitions."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    """Build the public CLI contract."""
    parser = argparse.ArgumentParser(
        prog="cursor-usage",
        description="Local metadata-only Cursor workload estimator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Initialize the tracker database")
    subparsers.add_parser("install-hook", help="Merge user-level Cursor hooks")
    subparsers.add_parser("hook", help=argparse.SUPPRESS)
    subparsers.add_parser("sync", help="Enrich records from Cursor's local DB")
    _add_snapshot_parser(subparsers)
    _add_calibration_parser(subparsers)
    _add_rate_parser(subparsers)
    _add_model_parser(subparsers)
    _add_telemetry_parser(subparsers)
    report = subparsers.add_parser("report", help="Show a usage report")
    report.add_argument("--date", help="Local date in YYYY-MM-DD format")
    report.add_argument("--start", help="Inclusive local date")
    report.add_argument("--end", help="Exclusive local date")
    report.add_argument("--json", action="store_true", help="Emit JSON")
    report.add_argument(
        "--daily",
        action="store_true",
        help="Show a table grouped by local date, repository, and model",
    )
    report.add_argument(
        "--accuracy",
        action="store_true",
        help="Compare estimates with imported request-level telemetry",
    )
    subparsers.add_parser("doctor", help="Check local data sources")
    return parser


def _add_snapshot_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser("snapshot", help="Record dashboard totals")
    parser.add_argument("action", choices=["add"])
    parser.add_argument("--captured-at")
    parser.add_argument("--total-events", type=int, required=True)
    parser.add_argument("--events-with-token", type=int, required=True)
    parser.add_argument("--events-without-token", type=int, required=True)
    parser.add_argument("--known-input", type=int, required=True)
    parser.add_argument("--known-output", type=int, required=True)
    parser.add_argument("--known-cache-read", type=int, required=True)
    parser.add_argument("--known-cache-write", type=int, required=True)


def _add_calibration_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser("calibration", help="Set model calibration")
    parser.add_argument("action", choices=["add"])
    parser.add_argument("model")
    for prefix in ("input", "output"):
        parser.add_argument(f"--{prefix}-point", type=int, required=True)
        parser.add_argument(f"--{prefix}-low", type=int, required=True)
        parser.add_argument(f"--{prefix}-high", type=int, required=True)
    parser.add_argument("--sample-size", type=int, required=True)


def _add_rate_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser("rate", help="Set effective-dated model rates")
    parser.add_argument("action", choices=["set"])
    parser.add_argument("model")
    parser.add_argument("--effective-at", required=True)
    parser.add_argument("--input-per-million", type=float, required=True)
    parser.add_argument("--output-per-million", type=float, required=True)
    parser.add_argument("--cache-read-per-million", type=float)
    parser.add_argument("--cache-write-per-million", type=float)


def _add_model_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        "model",
        help="Configure the assumed default model and reference pricing",
    )
    parser.add_argument("action", nargs="?", choices=["list", "show", "set"])
    parser.add_argument("model", nargs="?")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--repo", help="Apply to this repository label")
    scope.add_argument("--session", help="Apply to this conversation ID")
    parser.add_argument(
        "--effective-from",
        help="Timezone-aware ISO timestamp; defaults to now",
    )


def _add_telemetry_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        "telemetry",
        help="Import known token telemetry or inspect calibration profiles",
    )
    parser.add_argument("action", choices=["import", "profiles"])
    parser.add_argument("path", nargs="?")
