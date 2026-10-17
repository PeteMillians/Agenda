"""Role policy, unit-tested ahead of shared mode (spec §16)."""

import pytest

from app.domain.policy import Action, Role, allows


@pytest.mark.parametrize(
    ("role", "read", "write", "manage"),
    [
        (Role.OWNER, True, True, True),
        (Role.EDITOR, True, True, False),
        (Role.VIEWER, True, False, False),
    ],
)
def test_matrix(role, read, write, manage):
    assert allows(role, Action.READ) is read
    assert allows(role, Action.WRITE) is write
    assert allows(role, Action.MANAGE) is manage
