"""Reference-only model and pricing presets from Cursor documentation."""

from __future__ import annotations

from dataclasses import dataclass

REFERENCE_EFFECTIVE_AT = "2026-09-17T00:00:00+00:00"
REFERENCE_SOURCE_URL = "https://cursor.com/docs/models-and-pricing"
BASE_INPUT_RATE = 2.0
BASE_OUTPUT_RATE = 6.0


@dataclass(frozen=True, slots=True)
class ModelPreset:
    """One manually selected model with reference pricing."""

    slug: str
    display_name: str
    input_per_million: float
    output_per_million: float
    cache_read_per_million: float | None
    cache_write_per_million: float | None
    notes: str

    @property
    def input_multiplier(self) -> float:
        """Return the input rate relative to Grok 4.6 Standard."""
        return self.input_per_million / BASE_INPUT_RATE

    @property
    def output_multiplier(self) -> float:
        """Return the output rate relative to Grok 4.6 Standard."""
        return self.output_per_million / BASE_OUTPUT_RATE


MODEL_PRESETS = (
    ModelPreset(
        slug="cursor-grok-4.6-high-fast",
        display_name="Cursor Grok 4.6 High (Fast)",
        input_per_million=4.0,
        output_per_million=12.0,
        cache_read_per_million=1.0,
        cache_write_per_million=None,
        notes="Fast pricing; High effort has no separate fixed price multiplier.",
    ),
    ModelPreset(
        slug="claude-opus-5-thinking-high",
        display_name="Claude Opus 5 High",
        input_per_million=5.0,
        output_per_million=25.0,
        cache_read_per_million=0.5,
        cache_write_per_million=6.25,
        notes="Standard speed; no long-context surcharge in Cursor.",
    ),
    ModelPreset(
        slug="gpt-5.6-sol-medium",
        display_name="GPT-5.6 Sol Medium",
        input_per_million=4.0,
        output_per_million=20.0,
        cache_read_per_million=0.4,
        cache_write_per_million=5.0,
        notes="Long-context and Fast surcharges are not included.",
    ),
    ModelPreset(
        slug="claude-fable-5-thinking-high",
        display_name="Claude Fable 5 High",
        input_per_million=10.0,
        output_per_million=50.0,
        cache_read_per_million=1.0,
        cache_write_per_million=12.5,
        notes="Reference pricing is about twice Claude Opus 5.",
    ),
    ModelPreset(
        slug="gemini-3.8-flash-high",
        display_name="Gemini 3.8 Flash High",
        input_per_million=0.75,
        output_per_million=3.5,
        cache_read_per_million=0.075,
        cache_write_per_million=None,
        notes="No cache-write price is listed by Cursor.",
    ),
)
PRESETS_BY_SLUG = {preset.slug: preset for preset in MODEL_PRESETS}


def get_model_preset(slug: str) -> ModelPreset:
    """Return a known preset or raise an actionable validation error."""
    try:
        return PRESETS_BY_SLUG[slug]
    except KeyError as error:
        supported = ", ".join(PRESETS_BY_SLUG)
        raise ValueError(
            f"Unknown model {slug!r}. Supported models: {supported}"
        ) from error
