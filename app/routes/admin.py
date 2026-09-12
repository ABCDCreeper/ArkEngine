"""用户管理路由（契约 3.12）：按角色层级管理账号。"""
from flask import Blueprint, g, jsonify, request

from ..auth import require_auth, revoke_user_sessions
from ..db import commit, execute, gen_id, query_all, query_one, reset_demo
from ..errors import ApiError, bad_request, forbidden, not_found
from ..security import hash_password
from ..services import ROLE_RANK, add_audit, school_scope

bp = Blueprint('admin', __name__)

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
        (user_id, username, hash_password(password), name, target_role),
    )
    add_audit(g.user, 'user.create', 'user', user_id, f'创建 {target_role} 账号 {username}')
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
                (hash_password(str(body['password'])), user_id))
        # 旧口令已失效，同时吊销该用户已签发的会话，避免旧 token 继续可用
        revoke_user_sessions(user_id)
        add_audit(g.user, 'user.reset_password', 'user', user_id, f'重置 {target["username"]} 的口令')
    if body.get('role') is not None:
        target_role = body['role']
        if target_role not in ROLE_RANK or ROLE_RANK[target_role] >= ROLE_RANK[role]:
            raise bad_request('只能将角色调整为低于自己层级的角色')
        if target_role != target['role']:
            execute('UPDATE users SET role = ? WHERE id = ?', (target_role, user_id))
            add_audit(g.user, 'user.role_change', 'user', user_id,
                      f'{target["username"]} 的角色 {target["role"]} → {target_role}')
    if body.get('schoolId') is not None:
        school_id = body['schoolId'] or None
        if school_id and not query_one('SELECT 1 FROM schools WHERE id = ?', (school_id,)):
            raise bad_request('学校不存在')
        # 校管理员只能在自校范围内调整归属，不能把账号划到别的学校
        if ROLE_RANK[role] < ROLE_RANK['admin'] and school_id not in (None, school_scope(g.user)):
            raise forbidden('只能将账号归入本校')
        execute('UPDATE users SET schoolId = ? WHERE id = ?', (school_id, user_id))
        add_audit(g.user, 'user.school_change', 'user', user_id, f'归属学校设为 {school_id or "无"}')
    if body.get('name') is not None:
        add_audit(g.user, 'user.rename', 'user', user_id, f'姓名改为 {name}')
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
    execute('DELETE FROM risk_responses WHERE userId = ?', (user_id,))
    execute('DELETE FROM quiz_wrong_answers WHERE userId = ?', (user_id,))
    execute('DELETE FROM users WHERE id = ?', (user_id,))
    add_audit(g.user, 'user.delete', 'user', user_id, f'删除 {target["username"]}（{target["role"]}）')
    commit()
    return '', 204


# ---------------------------------------------------------------- 学校与演示数据

@bp.get('/admin/schools')
@require_auth
def list_schools():
    """学校列表：校管理员只看到自己的学校，平台管理员看到全部。"""
    role = require_admin_tier()
    scope = school_scope(g.user)
    if ROLE_RANK[role] >= ROLE_RANK['admin'] or scope is None:
        rows = query_all('SELECT * FROM schools ORDER BY name')
    else:
        rows = query_all('SELECT * FROM schools WHERE id = ?', (scope,))
    return jsonify({'items': rows, 'total': len(rows)})


@bp.get('/admin/audit-logs')
@require_auth
def audit_logs():
    """管理操作审计（校管理员及以上）：谁在何时对什么做了什么。

    校管理员只能看到自己范围相关的记录；平台管理员可见全部。
    """
    role = require_admin_tier()
    limit = min(max(int(request.args.get('limit', 50) or 50), 1), 200)
    if ROLE_RANK[role] >= ROLE_RANK['admin']:
        rows = query_all(
            'SELECT a.*, u.name AS actorName FROM audit_logs a LEFT JOIN users u ON u.id = a.actorId '
            'ORDER BY a.createdAt DESC LIMIT ?', (limit,))
    else:
        rows = query_all(
            'SELECT a.*, u.name AS actorName FROM audit_logs a LEFT JOIN users u ON u.id = a.actorId '
            'WHERE a.actorId = ? ORDER BY a.createdAt DESC LIMIT ?', (g.user['id'], limit))
    return jsonify({'items': rows, 'total': len(rows)})


@bp.post('/admin/demo/reset')
@require_auth
def demo_reset():
    """重置为初始演示数据（仅平台管理员）。

    反复演示时清掉上一轮留下的任务、批注、成绩；会删除所有账号，因此限最高层级。
    """
    if ROLE_RANK[g.user['role']] < ROLE_RANK['admin']:
        raise forbidden('仅平台管理员可重置演示数据')
    reset_demo()
    add_audit(g.user, 'demo.reset', 'database', 'demo', '重置为初始演示数据')
    commit()
    return jsonify({'reset': True})
