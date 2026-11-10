"""The registry of rewrite rules, and the driver every rule shares.

The driver (``RewriteRule`` and its helpers) is in ``kumosql.rule_driver``, which the equivalence prover
imports. This module adds what only the product needs: the registry that ``register_rule``,
``get_rule`` and ``available_rules`` keep, and BigQuery scripting, so a rule also runs on the statements
inside a ``BEGIN ... END`` block.
"""

from __future__ import annotations

from . import scripts as _scripts
from .lift_subqueries import LiftSubqueriesRule
from .rule_driver import (
    FATAL_DIAGNOSTIC_CODES,
    RewriteRule,
    RuleDiagnostic,
    RuleOutput,
    has_template_tags,
    set_script_support,
)

set_script_support(_scripts)

_REGISTRY: dict[str, RewriteRule] = {}


def register_rule(rule_class: type[RewriteRule]) -> type[RewriteRule]:
    """Class decorator that adds a rule to the global registry."""

    name = rule_class.name
    existing = _REGISTRY.get(name)
    if existing is not None and type(existing) is not rule_class:
        raise ValueError(f"rewrite rule `{name}` is already registered")
    _REGISTRY[name] = rule_class()
    return rule_class


def get_rule(name: str) -> RewriteRule:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise KeyError(f"unknown rewrite rule `{name}` (known rules: {known})") from None


def available_rules() -> dict[str, RewriteRule]:
    """Registered rules by name, in registration order."""

    return dict(_REGISTRY)


# ``lift_subqueries`` is defined with the prover's normalization; it is registered first, as it always was.
register_rule(LiftSubqueriesRule)

__all__ = [
    "FATAL_DIAGNOSTIC_CODES",
    "RewriteRule",
    "RuleDiagnostic",
    "RuleOutput",
    "available_rules",
    "get_rule",
    "has_template_tags",
    "register_rule",
]
