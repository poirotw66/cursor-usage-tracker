"""Transparent token and cost estimation primitives."""

from __future__ import annotations

import math
from pathlib import Path

from cursor_usage_tracker.domain import TokenEstimate, ToolOutputEstimate

FILE_SAMPLE_LIMIT_BYTES = 8 * 1024 * 1024
NON_TEXT_TOOL_NAME_MARKERS = ("screenshot", "capture_screenshot")
ASCII_CHARACTERS_PER_TOKEN = {
    "gpt": 4.0,
    "claude": 3.6,
    "gemini": 4.2,
    "grok": 3.8,
}


def estimate_text(text: str, model: str | None = None) -> TokenEstimate:
    """Estimate tokens while accounting for dense non-ASCII scripts."""
    family = _model_family(model)
    characters_per_token = ASCII_CHARACTERS_PER_TOKEN.get(family, 4.0)
    ascii_characters = sum(character.isascii() for character in text)
    non_ascii_characters = len(text) - ascii_characters
    tokens = math.ceil(ascii_characters / characters_per_token) + non_ascii_characters
    return TokenEstimate(
        characters=len(text),
        tokens=tokens,
        source=f"{family}_heuristic_v1",
    )


def estimate_file(
    path: Path,
    workspace_roots: tuple[Path, ...],
    model: str | None = None,
) -> TokenEstimate:
    """Estimate a referenced text file without retaining its contents."""
    resolved_path = path.expanduser().resolve(strict=True)
    if not resolved_path.is_file() or not _is_within_workspace(
        resolved_path, workspace_roots
    ):
        return TokenEstimate(characters=0, tokens=0)

    file_size = resolved_path.stat().st_size
    with resolved_path.open("rb") as file_handle:
        sample = file_handle.read(FILE_SAMPLE_LIMIT_BYTES)
    decoded_sample = sample.decode("utf-8", errors="replace")
    sample_estimate = estimate_text(decoded_sample, model)
    if not sample or file_size <= len(sample):
        return sample_estimate

    scale = file_size / len(sample)
    return TokenEstimate(
        characters=math.ceil(sample_estimate.characters * scale),
        tokens=math.ceil(sample_estimate.tokens * scale),
        source=sample_estimate.source,
    )


def estimate_tool_output(
    tool_name: str, output: str, model: str | None = None
) -> ToolOutputEstimate:
    """Estimate only tool results represented as observable text."""
    is_tokenizable_text = not any(
        marker in tool_name.casefold() for marker in NON_TEXT_TOOL_NAME_MARKERS
    )
    if not is_tokenizable_text:
        return ToolOutputEstimate(
            characters=len(output),
            tokens=0,
            is_tokenizable_text=False,
            source="excluded_non_text",
        )
    estimate = estimate_text(output, model)
    return ToolOutputEstimate(
        characters=estimate.characters,
        tokens=estimate.tokens,
        is_tokenizable_text=True,
        source=estimate.source,
    )


def _model_family(model: str | None) -> str:
    normalized = (model or "").casefold()
    for family in ASCII_CHARACTERS_PER_TOKEN:
        if family in normalized:
            return family
    return "generic"


def _is_within_workspace(path: Path, workspace_roots: tuple[Path, ...]) -> bool:
    for root in workspace_roots:
        try:
            path.relative_to(root.resolve(strict=True))
            return True
        except (OSError, ValueError):
            continue
    return False
