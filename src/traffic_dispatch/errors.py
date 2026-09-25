"""事故快处服务向 API 和 CLI 暴露的稳定错误。"""


class TrafficDispatchError(RuntimeError):
    code = "traffic_error"
    status = 400


class NotFound(TrafficDispatchError):
    code = "not_found"
    status = 404


class Conflict(TrafficDispatchError):
    code = "conflict"
    status = 409


class Forbidden(TrafficDispatchError):
    code = "forbidden"
    status = 403


class InvalidState(TrafficDispatchError):
    code = "invalid_state"
    status = 409


class ValidationFailed(TrafficDispatchError):
    code = "validation_failed"
    status = 422
