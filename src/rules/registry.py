"""
Pluggable action registry — maps action_type strings to handler classes.
"""


from __future__ import annotations
from typing import Type


class RuleActionHandler:
    """Base class for rule action handlers."""

    action_type: str = ""

    async def execute(self, action, context) -> None:
        raise NotImplementedError


_registry: dict[str, Type[RuleActionHandler]] = {}


def register(action_type: str, handler_cls: Type[RuleActionHandler]):
    _registry[action_type] = handler_cls


def get(action_type: str) -> Type[RuleActionHandler]:
    if action_type not in _registry:
        raise KeyError(f"Unknown rule action: '{action_type}'")
    return _registry[action_type]


def list_actions() -> list[str]:
    return sorted(_registry.keys())
