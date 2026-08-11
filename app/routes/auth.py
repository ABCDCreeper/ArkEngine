"""认证路由（契约 3.1）：登录 / 登出 / 当前用户。"""
from flask import Blueprint, g, jsonify, request
from werkzeug.security import check_password_hash

from ..auth import create_session, delete_session, require_auth
from ..db import query_one
from ..errors import ApiError, bad_request, get_json_body
from ..services import user_brief

bp = Blueprint('auth', __name__)


@bp.post('/sessions')
def login():
    body = get_json_body()
    username = body.get('username')
    password = body.get('password')
    if not username or not password:
        raise bad_request('用户名和密码不能为空')
    user = query_one('SELECT * FROM users WHERE username = ?', (username,))
    if not user or not check_password_hash(user['password'], password):
        raise ApiError(401, 'INVALID_CREDENTIALS', '用户名或密码错误')
    return jsonify({'token': create_session(user['id']), 'user': user_brief(user)}), 201


@bp.delete('/sessions/current')
@require_auth
def logout():
    authorization = request.headers.get('Authorization', '')
    token = authorization[7:] if authorization.startswith('Bearer ') else authorization
    delete_session(token)
    return '', 204


@bp.get('/me')
@require_auth
def me():
    return jsonify({'user': user_brief(g.user)})
