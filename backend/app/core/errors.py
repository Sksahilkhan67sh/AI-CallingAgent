"""Small set of domain exceptions mapped to HTTP responses in main.py.
Not a large custom exception framework -- just enough to give API
consumers a consistent, meaningful error shape."""


class NotFoundError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class ConflictError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class ValidationError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)
