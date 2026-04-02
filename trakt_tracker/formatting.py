from __future__ import annotations


def format_compact_votes(value: int | None) -> str:
    if value is None:
        return ""
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "m"


def format_rating_with_votes(rating: float | None, votes: int | None) -> str:
    if rating is None:
        return "n/a"
    compact_votes = format_compact_votes(votes)
    if compact_votes:
        return f"{rating:.1f} ({compact_votes})"
    return f"{rating:.1f}"


def format_progress_percent(value: float | None) -> str:
    if value is None:
        return "0%"
    return f"{int(round(value))}%"
