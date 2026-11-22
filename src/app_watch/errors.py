"""User-facing configuration diagnostics."""

from typing import Self


class ConfigurationError(ValueError):
    """Invalid or inaccessible watch configuration."""

    summary: str | None = None

    @classmethod
    def with_summary(cls, summary: str, *, detail: str | None = None) -> Self:
        """Opt in to a summary safe to show outside the watch account."""
        error = cls(summary if detail is None else detail)
        error.summary = summary
        return error


class StateError(RuntimeError):
    """Unusable recovery evidence; refuse execution rather than repair it."""


class BlockedError(RuntimeError):
    """Execution or lifecycle management cannot safely proceed."""
