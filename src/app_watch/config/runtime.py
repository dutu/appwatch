"""Explicit parameters for instance paths and action arguments; no shell expansion."""

import re
from collections.abc import Mapping, Sequence
from string import Formatter

from app_watch.errors import ConfigurationError

PARAMETER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PARAMETER_VALUE = re.compile(r"^[A-Za-z0-9_.:@+-]+$")
DATA_DIR_PLACEHOLDER = "application.data_dir"
JOB_ID_PLACEHOLDER = "job.id"


def parse_parameters(arguments: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for argument in arguments:
        key, separator, value = argument.partition("=")
        if not separator or not PARAMETER_NAME.fullmatch(key):
            raise ConfigurationError.with_summary(
                "Runtime parameter must use NAME=VALUE with a valid name"
            )
        if key in result:
            raise ConfigurationError.with_summary(
                "Duplicate runtime parameter", detail=f"Duplicate runtime parameter: {key}"
            )
        if not PARAMETER_VALUE.fullmatch(value):
            raise ConfigurationError.with_summary(
                "Runtime parameter value is empty or contains unsupported characters",
                detail=f"Parameter {key}: use nonempty letters/digits "
                "or '.', '_', ':', '@', '+', '-'",
            )
        result[key] = value
    return result


def render(value: str, parameters: Mapping[str, str], used: set[str]) -> str:
    """Allow named parameters and fixed built-in lookups, never attribute evaluation."""
    output: list[str] = []
    try:
        for literal, name, spec, conversion in Formatter().parse(value):
            output.append(literal)
            if name is None:
                continue
            if (
                (
                    not PARAMETER_NAME.fullmatch(name)
                    and name not in (DATA_DIR_PLACEHOLDER, JOB_ID_PLACEHOLDER)
                )
                or spec
                or conversion
            ):
                raise ConfigurationError.with_summary(
                    "Use plain {NAME}, {application.data_dir}, or {job.id} placeholders only"
                )
            if name not in parameters:
                raise ConfigurationError.with_summary(
                    "Missing runtime parameter", detail=f"Missing runtime parameter: {name}"
                )
            used.add(name)
            output.append(parameters[name])
    except ValueError as error:
        if isinstance(error, ConfigurationError):
            raise
        raise ConfigurationError.with_summary(
            "Invalid parameter template", detail=f"Invalid parameter template: {error}"
        ) from error
    return "".join(output)
