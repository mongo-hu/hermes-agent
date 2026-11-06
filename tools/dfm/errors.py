"""Stable error contracts for the DFM toolset."""

from __future__ import annotations

from typing import Any, Mapping


class DFMError(Exception):
    """A user-actionable DFM failure with a machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


class ReviewSaveError(DFMError):
    """An accepted review intent failed before its durable commit."""

    @classmethod
    def from_error(cls, error: Exception, edit_id: str) -> "ReviewSaveError":
        return cls(
            getattr(error, "code", "review_save_failed"), str(error),
            {**getattr(error, "details", {}), "failure_category": "save_failed", "edit_id": edit_id},
        )


class ManifestCommitError(DFMError):
    """Validation succeeded, but writing the durable event failed."""
