"""认证路由（契约 3.1）：注册 / 登录 / 登出 / 当前用户。"""
from flask import Blueprint, g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from ..auth import create_session, delete_session, require_auth
from ..db import commit, execute, gen_id, query_one
from ..errors import ApiError, bad_request, get_json_body
from ..services import user_brief

bp = Blueprint('auth', __name__)


@bp.post('/users')
def register():
    """注册（公开接口）：注册成功即登录态，直接返回 token（契约 3.1）。"""
    body = get_json_body()
    username = body.get('username')
    password = body.get('password')
    name = body.get('name')
    role = body.get('role')
    if not username or not password or not name:
        raise bad_request('用户名、密码和姓名不能为空')
    if len(str(username)) < 3:
        raise bad_request('用户名至少 3 个字符')
    if len(str(password)) < 6:
        raise bad_request('密码至少 6 个字符')
    if role not in ('student', 'teacher'):
        raise bad_request('角色只能是 student 或 teacher')
    if query_one('SELECT 1 FROM users WHERE username = ?', (username,)):
        raise ApiError(409, 'USERNAME_TAKEN', '用户名已被占用')
    user = {'id': gen_id('u'), 'username': username, 'name': name, 'role': role}
    execute('INSERT INTO users (id, username, password, name, role) VALUES (?, ?, ?, ?, ?)',
            (user['id'], username, generate_password_hash(password), name, role))
    commit()
    return jsonify({'token': create_session(user['id']), 'user': user}), 201


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
