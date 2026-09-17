"""Manual default-model mapping and reference pricing setup."""

from __future__ import annotations

from datetime import UTC, datetime

from cursor_usage_tracker.model_assignment_storage import ModelAssignmentRepository
from cursor_usage_tracker.model_presets import (
    MODEL_PRESETS,
    REFERENCE_EFFECTIVE_AT,
    REFERENCE_SOURCE_URL,
    ModelPreset,
    get_model_preset,
)
from cursor_usage_tracker.storage import Storage


def configure_default_model(
    storage: Storage,
    slug: str,
    *,
    scope_type: str = "global",
    scope_value: str = "*",
    effective_from: datetime | None = None,
) -> ModelPreset:
    """Save a manual model assumption and install its reference rate."""
    preset = get_model_preset(slug)
    now = datetime.now(UTC)
    starts_at = effective_from or now
    if starts_at.tzinfo is None:
        raise ValueError("Model assignment effective time must include a timezone")
    ModelAssignmentRepository(storage).add(
        model=preset.slug,
        scope_type=scope_type,
        scope_value=scope_value,
        effective_from=starts_at.astimezone(UTC).isoformat(),
        created_at=now.isoformat(),
    )
    storage.add_rate(
        model=preset.slug,
        effective_at=REFERENCE_EFFECTIVE_AT,
        input_per_million=preset.input_per_million,
        output_per_million=preset.output_per_million,
        cache_read_per_million=preset.cache_read_per_million,
        cache_write_per_million=preset.cache_write_per_million,
    )
    return preset


def get_configured_model(storage: Storage) -> ModelPreset | None:
    """Return the configured default model when present and supported."""
    assignment = ModelAssignmentRepository(storage).latest(
        scope_type="global",
        scope_value="*",
    )
    slug = str(assignment["model"]) if assignment is not None else None
    return get_model_preset(slug) if slug is not None else None


def render_model_presets(configured_slug: str | None = None) -> str:
    """Render available reference presets and separate price multipliers."""
    lines = [
        "Reference model presets",
        "Pricing reference only; not Cursor billing telemetry.",
        "Baseline: Grok 4.6 Standard ($2/M input, $6/M output)",
        "",
    ]
    for index, preset in enumerate(MODEL_PRESETS, start=1):
        selected = " [configured]" if preset.slug == configured_slug else ""
        lines.extend(
            [
                f"{index}. {preset.display_name}{selected}",
                f"   {preset.slug}",
                (
                    f"   Input ${preset.input_per_million:g}/M "
                    f"(×{preset.input_multiplier:.3g}); "
                    f"Output ${preset.output_per_million:g}/M "
                    f"(×{preset.output_multiplier:.3g})"
                ),
                f"   {preset.notes}",
            ]
        )
    lines.extend(["", f"Source: {REFERENCE_SOURCE_URL}"])
    return "\n".join(lines)
