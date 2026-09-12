"""认证路由（契约 3.1）：注册 / 登录 / 登出 / 当前用户。"""
from flask import Blueprint, g, jsonify, request

from ..auth import create_session, delete_session, require_auth
from ..db import commit, execute, gen_id, query_one
from ..errors import ApiError, bad_request, get_json_body
from ..security import hash_password, verify_password
from ..services import user_brief
from ..validation import text_value

bp = Blueprint('auth', __name__)


@bp.post('/users')
def register():
    """注册（公开接口）：注册成功即登录态，直接返回 token（契约 3.1）。"""
    body = get_json_body()
    username = text_value(body.get('username'), '用户名', 80, 3)
    password = text_value(body.get('password'), '密码', 256, 6, strip=False)
    name = text_value(body.get('name'), '姓名', 80, 1)
    role = body.get('role')
    if not username or not password or not name:
        raise bad_request('用户名、密码和姓名不能为空')
    if len(str(username)) < 3:
        raise bad_request('用户名至少 3 个字符')
    if len(str(password)) < 6:
        raise bad_request('密码至少 6 个字符')
    if role != 'student':
        raise bad_request('公开注册仅支持学生，教师账号由管理员创建')
    if query_one('SELECT 1 FROM users WHERE username = ?', (username,)):
        raise ApiError(409, 'USERNAME_TAKEN', '用户名已被占用')
    user = {'id': gen_id('u'), 'username': username, 'name': name, 'role': role}
    execute('INSERT INTO users (id, username, password, name, role) VALUES (?, ?, ?, ?, ?)',
            (user['id'], username, hash_password(password), name, role))
    commit()
    return jsonify({'token': create_session(user['id']), 'user': user}), 201


@bp.post('/sessions')
def login():
    body = get_json_body()
    username = text_value(body.get('username'), '用户名', 80, 1)
    password = text_value(body.get('password'), '密码', 256, 1, strip=False)
    if not username or not password:
        raise bad_request('用户名和密码不能为空')
    user = query_one('SELECT * FROM users WHERE username = ?', (username,))
    if not user or not verify_password(user['password'], password):
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
