class ApiError(Exception):
    def __init__(self, status: int, message: str, kind: str = 'bad_request_error'):
        super().__init__(message)
        self.status, self.message, self.kind = status, message, kind
