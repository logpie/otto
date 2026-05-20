"""Shared click option validators (used by `otto`, `otto improve`, etc.)."""

from __future__ import annotations

import click


def positive_budget_option(
    _ctx: click.Context,
    _param: click.Parameter,
    value: int | None,
) -> int | None:
    """`--budget`: positive wall-clock budget in seconds."""
    if value is not None and value <= 0:
        raise click.BadParameter("must be > 0")
    return value


def rounds_option(
    _ctx: click.Context,
    _param: click.Parameter,
    value: int | None,
) -> int | None:
    """`--rounds`: 1..50."""
    if value is None:
        return None
    if value <= 0:
        raise click.BadParameter("must be >= 1")
    if value > 50:
        raise click.BadParameter("must be <= 50")
    return value


def max_turns_option(
    _ctx: click.Context,
    _param: click.Parameter,
    value: int | None,
) -> int | None:
    """`--max-turns`: 1..200 (per agent call)."""
    if value is None:
        return None
    if value < 1:
        raise click.BadParameter("must be >= 1")
    if value > 200:
        raise click.BadParameter("must be <= 200")
    return value
