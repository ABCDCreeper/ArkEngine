"""用户管理路由（契约 3.12）：按角色层级管理账号。"""
from flask import Blueprint, g, jsonify, request
from werkzeug.security import generate_password_hash

from ..auth import require_auth
from ..db import commit, execute, gen_id, query_all, query_one
from ..errors import ApiError, bad_request, forbidden, not_found

bp = Blueprint('admin', __name__)

ROLE_RANK = {'student': 0, 'teacher': 1, 'schooladmin': 2, 'admin': 3, 'superadmin': 4}

RANK_CASE = (
    "CASE role WHEN 'superadmin' THEN 4 WHEN 'admin' THEN 3 "
    "WHEN 'schooladmin' THEN 2 WHEN 'teacher' THEN 1 ELSE 0 END"
)


def require_admin_tier() -> str:
    """仅校管理员及以上可访问，返回调用者角色。"""
    role = g.user['role']
    if ROLE_RANK.get(role, -1) < ROLE_RANK['schooladmin']:
        raise forbidden('仅管理角色可执行此操作')
    return role


def user_view(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != 'password'}


def visible_roles(caller_role: str) -> list[str]:
    max_rank = ROLE_RANK[caller_role] - 1
    return [r for r, rank in ROLE_RANK.items() if rank <= max_rank]


@bp.get('/admin/users')
@require_auth
def list_users():
    """用户列表：可见范围随层级收窄（不含自己与更高层）。"""
    role = require_admin_tier()
    roles = visible_roles(role)
    placeholders = ','.join('?' * len(roles))
    keyword = request.args.get('keyword', '').strip().lower()
    if keyword:
        rows = query_all(
            f"SELECT * FROM users WHERE role IN ({placeholders}) AND "
            '(LOWER(username) LIKE ? OR LOWER(name) LIKE ?) ORDER BY ' + RANK_CASE + ' DESC, name',
            [*roles, f'%{keyword}%', f'%{keyword}%'],
        )
    else:
        rows = query_all(
            f'SELECT * FROM users WHERE role IN ({placeholders}) ORDER BY ' + RANK_CASE + ' DESC, name',
            roles,
        )
    rows = [r for r in rows if r['id'] != g.user['id']]
    return jsonify({'items': [user_view(r) for r in rows], 'total': len(rows)})


@bp.post('/admin/users')
@require_auth
def create_user():
    """创建低于自己层级的账号（管理角色专属）。"""
    role = require_admin_tier()
    body = request.get_json(silent=True) or {}
    username = str(body.get('username') or '').strip()
    password = str(body.get('password') or '')
    name = str(body.get('name') or '').strip()
    target_role = body.get('role')
    if len(username) < 3 or len(password) < 6 or not name:
        raise bad_request('用户名至少 3 个字符、密码至少 6 个字符、姓名必填')
    if target_role not in ROLE_RANK or ROLE_RANK[target_role] >= ROLE_RANK[role]:
        raise bad_request('只能创建低于自己层级的账号')
    if query_one('SELECT 1 FROM users WHERE username = ?', (username,)):
        raise ApiError(409, 'USERNAME_TAKEN', '用户名已被占用')
    user_id = gen_id('u')
    execute(
        'INSERT INTO users (id, username, password, name, role) VALUES (?, ?, ?, ?, ?)',
        (user_id, username, generate_password_hash(password), name, target_role),
    )
    commit()
    return jsonify(user_view(query_one('SELECT * FROM users WHERE id = ?', (user_id,)))), 201


@bp.patch('/admin/users/<user_id>')
@require_auth
def update_user(user_id):
    """改名 / 重置密码 / 调整角色（目标须低于自己层级，不能改自己）。"""
    role = require_admin_tier()
    if user_id == g.user['id']:
        raise bad_request('不能修改自己的账号')
    target = query_one('SELECT * FROM users WHERE id = ?', (user_id,))
    if not target:
        raise not_found('用户不存在')
    if ROLE_RANK[target['role']] >= ROLE_RANK[role]:
        raise forbidden('不能管理同级或更高层级的账号')
    body = request.get_json(silent=True) or {}
    if body.get('name') is not None:
        name = str(body['name']).strip()
        if not name:
            raise bad_request('姓名不能为空')
        execute('UPDATE users SET name = ? WHERE id = ?', (name, user_id))
    if body.get('password') is not None:
        if len(str(body['password'])) < 6:
            raise bad_request('密码至少 6 个字符')
        execute('UPDATE users SET password = ? WHERE id = ?',
                (generate_password_hash(str(body['password'])), user_id))
    if body.get('role') is not None:
        target_role = body['role']
        if target_role not in ROLE_RANK or ROLE_RANK[target_role] >= ROLE_RANK[role]:
            raise bad_request('只能将角色调整为低于自己层级的角色')
        execute('UPDATE users SET role = ? WHERE id = ?', (target_role, user_id))
    commit()
    return jsonify(user_view(query_one('SELECT * FROM users WHERE id = ?', (user_id,))))


@bp.delete('/admin/users/<user_id>')
@require_auth
def delete_user(user_id):
    """删除低于自己层级的账号（级联清理关联数据）。"""
    role = require_admin_tier()
    if user_id == g.user['id']:
        raise bad_request('不能删除自己的账号')
    target = query_one('SELECT * FROM users WHERE id = ?', (user_id,))
    if not target:
        raise not_found('用户不存在')
    if ROLE_RANK[target['role']] >= ROLE_RANK[role]:
        raise forbidden('不能管理同级或更高层级的账号')
    execute('UPDATE tasks SET assigneeId = NULL WHERE assigneeId = ?', (user_id,))
    execute('DELETE FROM members WHERE userId = ?', (user_id,))
    execute('DELETE FROM group_members WHERE userId = ?', (user_id,))
    execute('DELETE FROM group_invites WHERE userId = ? OR inviterId = ?', (user_id, user_id))
    execute('DELETE FROM sessions WHERE userId = ?', (user_id,))
    execute('DELETE FROM focus_sessions WHERE userId = ?', (user_id,))
    execute('DELETE FROM quiz_attempts WHERE userId = ?', (user_id,))
    execute('DELETE FROM checkins WHERE userId = ?', (user_id,))
    execute('DELETE FROM feedbacks WHERE userId = ?', (user_id,))
    execute('DELETE FROM annotations WHERE userId = ?', (user_id,))
    execute('DELETE FROM users WHERE id = ?', (user_id,))
    commit()
    return '', 204
