from __future__ import annotations


class AlertCompileError(ValueError):
    def __init__(self, code: str, message: str, *, suggestion: str | None = None):
        super().__init__(message)
        self.code = str(code)
        self.suggestion = suggestion


class AlertValidationError(ValueError):
    def __init__(self, code: str, message: str, *, suggestion: str | None = None):
        super().__init__(message)
        self.code = str(code)
        self.suggestion = suggestion
