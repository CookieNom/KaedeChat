from __future__ import annotations


class KaedeError(Exception):
    pass


class ApiError(KaedeError):
    def __init__(self, status: int, code: str, message: str, detail: object = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


class Forbidden(ApiError):
    pass


class NotFound(ApiError):
    pass


class RateLimited(ApiError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        retry_after: float,
        detail: object = None,
    ):
        super().__init__(status, code, message, detail)
        self.retry_after = retry_after
