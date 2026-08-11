"""鉴权：服务端 session token（登录签发、登出删除，契约 3.1）。"""
import secrets
from functools import wraps

from flask import g, request

from .db import commit, execute, gen_id, now_iso, query_one
from .errors import unauthorized


def create_session(user_id: str) -> str:
    """登录成功后签发 token 并入库。"""
    token = secrets.token_urlsafe(32)
    execute(
        'INSERT INTO sessions (id, userId, token, createdAt) VALUES (?, ?, ?, ?)',
        (gen_id('s'), user_id, token, now_iso()),
    )
    commit()
    return token


def delete_session(token: str) -> None:
    """登出：删除会话使 token 失效。"""
    execute('DELETE FROM sessions WHERE token = ?', (token,))
    commit()


def parse_token(authorization: str | None) -> dict | None:
    """从 Authorization: Bearer <token> 解析当前用户（不含 password）。"""
    if not authorization:
        return None
    token = authorization[7:] if authorization.startswith('Bearer ') else authorization
    row = query_one(
        'SELECT u.id, u.username, u.name, u.role FROM sessions s '
        'JOIN users u ON u.id = s.userId WHERE s.token = ?',
        (token,),
    )
    return row


def require_auth(fn):
    """视图装饰器：未登录返回 401 UNAUTHORIZED，登录后注入 g.user。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = parse_token(request.headers.get('Authorization'))
        if not user:
            raise unauthorized()
        g.user = user
        return fn(*args, **kwargs)

    return wrapper
