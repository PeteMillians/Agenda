"""Role policy (spec §06). v1 only ever creates owners; editor/viewer are tested ahead of shared mode."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class Action(StrEnum):
    READ = "read"  # any data in the space
    WRITE = "write"  # create/edit/delete events, tasks, lists, people
    MANAGE = "manage"  # space settings, archive, membership


ALLOWED: dict[Role, frozenset[Action]] = {
    Role.OWNER: frozenset(Action),
    Role.EDITOR: frozenset({Action.READ, Action.WRITE}),
    Role.VIEWER: frozenset({Action.READ}),
}


def allows(role: Role, action: Action) -> bool:
    return action in ALLOWED[role]
