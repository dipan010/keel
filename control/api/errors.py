"""Errors a developer can act on.

Every message names what was wrong and, where there is one, what to do instead.
An error that says "invalid request" costs someone twenty minutes.
"""

from __future__ import annotations

from fastapi import HTTPException


class Conflict(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(409, detail)


class QuotaExceeded(HTTPException):
    def __init__(self, requested: int, in_use: int, quota: int) -> None:
        super().__init__(
            409,
            f"team GPU quota exceeded: {in_use} of {quota} in use, this needs "
            f"{requested} more. Delete an unused deployment or ask for more quota.",
        )
