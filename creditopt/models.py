"""Model catalogue: tiers, relative cost, and routing metadata.

Prices are Anthropic first-party API rates in USD per million tokens. On a
Pro/Max subscription you don't pay these directly, but usage limits scale with
the compute you consume, so API-equivalent cost is a good common currency for
"how much of my allowance did this use".
"""

from dataclasses import dataclass

# When the prices below were last checked against Anthropic's published API
# pricing. Update this and the CATALOGUE together when prices change.
PRICES_CHECKED = "2026-10-04"

# Cache writes cost 1.25x input for the 5-minute TTL and 2x for the 1-hour TTL.
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0


@dataclass(frozen=True)
class Model:
    key: str            # family key used throughout the app
    name: str
    model_id: str
    cli_alias: str      # what to pass to `claude --model` / `/model`
    tier: int           # 1 = cheapest
    input: float
    output: float
    cache_read: float
    context: int
    good_for: str


CATALOGUE = [
    Model("haiku", "Haiku 4.5", "claude-haiku-4-5", "haiku", 1,
          1.00, 5.00, 0.10, 200_000,
          "Lookups, searches, renames, formatting, quick questions, codebase exploration"),
    Model("sonnet", "Sonnet 5.5", "claude-sonnet-5-5", "sonnet", 2,
          2.00, 10.00, 0.20, 1_000_000,
          "Everyday coding: features, bug fixes, tests, refactors within a few files"),
    Model("opus", "Opus 5.5", "claude-opus-5-5", "opus", 3,
          4.00, 20.00, 0.20, 1_000_000,
          "Architecture, planning, hard debugging, cross-cutting changes, reviews"),
    Model("fable", "Fable 5.1", "claude-fable-5-1", "claude-fable-5-1", 4,
          10.00, 50.00, 0.25, 1_000_000,
          "The hardest long-horizon reasoning when Opus has already struggled"),
]

BY_KEY = {m.key: m for m in CATALOGUE}

# Older ids still show up in historic transcripts; price them as their family.
_FAMILY_HINTS = [("fable", "fable"), ("mythos", "fable"), ("opus", "opus"),
                 ("sonnet", "sonnet"), ("haiku", "haiku")]


def family(model_id):
    """Map any model id ('claude-opus-4-8', 'claude-sonnet-5-5[1m]') to a family key."""
    mid = (model_id or "").lower()
    for hint, key in _FAMILY_HINTS:
        if hint in mid:
            return key
    return "other"


def lookup(model_id):
    """Catalogue entry for a model id, falling back to Sonnet pricing for unknowns."""
    return BY_KEY.get(family(model_id), BY_KEY["sonnet"])


def cost(model_id, input_tokens=0, output_tokens=0, cache_read=0,
         cache_write_5m=0, cache_write_1h=0):
    """API-equivalent cost in USD."""
    m = lookup(model_id)
    return (input_tokens * m.input
            + output_tokens * m.output
            + cache_read * m.cache_read
            + cache_write_5m * m.input * CACHE_WRITE_5M
            + cache_write_1h * m.input * CACHE_WRITE_1H) / 1_000_000
