"""Refusal type shared by every layer of the bridge."""

from __future__ import annotations

# error code -> HTTP status used by the plain-JSON endpoints
HTTP_STATUS = {
    "invalid_argument": 400,
    "invalid_message_id": 400,
    "invalid_command": 400,
    "not_allowed": 403,
    "denied": 403,
    "self_message": 403,
    "stale_message": 403,
    "chat_mismatch": 403,
    "message_not_found": 404,
    "rate_limited": 429,
    "unsupported_message": 422,
    "platform_unavailable": 503,
    "internal_error": 500,
}


class BridgeError(Exception):
    """A refusal with a stable machine-readable ``code`` and a human message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 400)

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message}
