"""Explicit parameters for instance paths and action arguments; no shell expansion."""

import re
from collections.abc import Mapping, Sequence
from string import Formatter

from app_watch.errors import ConfigurationError

PARAMETER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PARAMETER_VALUE = re.compile(r"^[A-Za-z0-9_.:@+-]+$")


def parse_parameters(arguments: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for argument in arguments:
        key, separator, value = argument.partition("=")
        if not separator or not PARAMETER_NAME.fullmatch(key):
            raise ConfigurationError("Runtime parameter must use NAME=VALUE with a valid name")
        if key in result:
            raise ConfigurationError(f"Duplicate runtime parameter: {key}")
        if not PARAMETER_VALUE.fullmatch(value):
            raise ConfigurationError(
                f"Parameter {key}: use nonempty letters/digits or '.', '_', ':', '@', '+', '-'"
            )
        result[key] = value
    return result


def render(value: str, parameters: Mapping[str, str], used: set[str]) -> str:
    """Allow simple named fields and escaped braces, never attributes or formatting."""
    output: list[str] = []
    try:
        for literal, name, spec, conversion in Formatter().parse(value):
            output.append(literal)
            if name is None:
                continue
            if not PARAMETER_NAME.fullmatch(name) or spec or conversion:
                raise ConfigurationError("Use plain {NAME} parameter placeholders only")
            if name not in parameters:
                raise ConfigurationError(f"Missing runtime parameter: {name}")
            used.add(name)
            output.append(parameters[name])
    except ValueError as error:
        if isinstance(error, ConfigurationError):
            raise
        raise ConfigurationError(f"Invalid parameter template: {error}") from error
    return "".join(output)
