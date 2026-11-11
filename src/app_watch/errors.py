"""User-facing configuration diagnostics."""


class ConfigurationError(ValueError):
    """Invalid or inaccessible watch configuration."""


class StateError(RuntimeError):
    """Unusable recovery evidence; refuse execution rather than repair it."""


class BlockedError(RuntimeError):
    """Execution or lifecycle management cannot safely proceed."""
