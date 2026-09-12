"""统一错误处理：ApiError + 错误响应格式（契约 1.3 / 1.4）。"""
from flask import Flask, jsonify, request


class ApiError(Exception):
    """带 HTTP 状态码与业务错误码的异常。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def bad_request(message: str = '请求参数错误') -> ApiError:
    return ApiError(400, 'VALIDATION_ERROR', message)


def unauthorized(message: str = '未登录或登录已过期') -> ApiError:
    return ApiError(401, 'UNAUTHORIZED', message)


def forbidden(message: str = '无权限执行此操作') -> ApiError:
    return ApiError(403, 'FORBIDDEN', message)


def not_found(message: str = '资源不存在') -> ApiError:
    return ApiError(404, 'NOT_FOUND', message)


def get_json_body() -> dict:
    """解析请求体；缺失或非法 JSON 一律 400（契约 1.3）。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise bad_request('请求体必须为 JSON 对象')
    return data


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def handle_api_error(e: ApiError):
        return jsonify({'error': {'code': e.code, 'message': e.message}}), e.status

    @app.errorhandler(404)
    def handle_not_found(_e):
        return jsonify({'error': {'code': 'NOT_FOUND', 'message': '接口不存在'}}), 404

    # 方法不允许时与 Mock 一致返回 404 NOT_FOUND
    @app.errorhandler(405)
    def handle_method_not_allowed(_e):
        return jsonify({'error': {'code': 'NOT_FOUND', 'message': f'接口不存在: {request.method} {request.path}'}}), 404

    @app.errorhandler(Exception)
    def handle_unexpected(e):
        app.logger.error('Unhandled error: %s', e, exc_info=True)
        return jsonify({'error': {'code': 'INTERNAL_ERROR', 'message': '服务器内部错误'}}), 500
