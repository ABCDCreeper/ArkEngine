"""用户组与分组题库管理路由（契约 3.11）：教师维护分组、成员与题库。"""
from flask import Blueprint, g, jsonify, request

from ..auth import require_auth
from ..db import commit, execute, gen_group_invite_code, gen_id, json_dumps, json_loads, now_iso, query_all, query_one
from ..errors import ApiError, bad_request, forbidden, not_found

bp = Blueprint('groups', __name__)

QUIZ_MODES = ('group', 'fallback', 'mixed')


def require_teacher() -> None:
    if g.user['role'] != 'teacher':
        raise forbidden('仅教师可执行此操作')


def get_group_or_404(group_id: str) -> dict:
    group = query_one('SELECT * FROM groups WHERE id = ?', (group_id,))
    if not group:
        raise ApiError(404, 'GROUP_NOT_FOUND', '用户组不存在')
    return group


def require_manager(group_id: str) -> None:
    if not query_one(
        "SELECT 1 FROM group_members WHERE groupId = ? AND userId = ? AND role = 'teacher'",
        (group_id, g.user['id']),
    ):
        raise forbidden('仅该组的负责老师可操作')


def group_view(group: dict) -> dict:
    return {
        **group,
        'memberCount': query_one('SELECT COUNT(*) AS c FROM group_members WHERE groupId = ?', (group['id'],))['c'],
        'questionCount': query_one('SELECT COUNT(*) AS c FROM quiz_questions WHERE groupId = ?', (group['id'],))['c'],
        'projectCount': query_one('SELECT COUNT(*) AS c FROM projects WHERE groupId = ?', (group['id'],))['c'],
    }


def question_view(row: dict) -> dict:
    return {**row, 'options': json_loads(row['options'])}


def validate_question(body: dict) -> dict:
    question = str(body.get('question', '')).strip()
    category = str(body.get('category', '')).strip()
    difficulty = body.get('difficulty', 1)
    options = body.get('options')
    answer = body.get('answer')
    explanation = str(body.get('explanation', '')).strip()
    if not question or len(question) > 200:
        raise bad_request('题目不能为空且不超过 200 字')
    if not category or len(category) > 20:
        raise bad_request('分类不能为空')
    if not isinstance(difficulty, int) or not 1 <= difficulty <= 3:
        raise bad_request('难度需为 1~3 的整数')
    if not isinstance(options, list) or len(options) != 4 or not all(isinstance(o, str) and o.strip() for o in options):
        raise bad_request('options 需为 4 个非空选项')
    if not isinstance(answer, int) or not 0 <= answer <= 3:
        raise bad_request('answer 需为 0~3 的整数')
    if not explanation:
        raise bad_request('解析不能为空')
    return {
        'question': question,
        'category': category,
        'difficulty': difficulty,
        'options': [o.strip() for o in options],
        'answer': answer,
        'explanation': explanation,
    }


# ---------------------------------------------------------------- 用户组

@bp.get('/groups')
@require_auth
def list_groups():
    """教师：我负责管理的用户组（含成员数/题数/抽题机制）。"""
    require_teacher()
    rows = query_all(
        'SELECT g.* FROM group_members gm JOIN groups g ON g.id = gm.groupId '
        "WHERE gm.userId = ? AND gm.role = 'teacher' ORDER BY g.updatedAt DESC",
        (g.user['id'],),
    )
    return jsonify({'items': [group_view(r) for r in rows], 'total': len(rows)})


@bp.get('/groups/mine')
@require_auth
def my_groups():
    """学生：我所在的用户组列表（含抽题机制与统计，多组并列；不暴露邀请码）。"""
    rows = query_all(
        'SELECT g.* FROM group_members gm JOIN groups g ON g.id = gm.groupId '
        "WHERE gm.userId = ? AND gm.role = 'member' ORDER BY g.name",
        (g.user['id'],),
    )
    items = [group_view(r) for r in rows]
    for item in items:
        item.pop('inviteCode', None)
    return jsonify({'items': items, 'total': len(items)})


@bp.post('/groups')
@require_auth
def create_group():
    require_teacher()
    body = request.get_json(silent=True) or {}
    name = str(body.get('name', '')).strip()
    if not name or len(name) > 50:
        raise bad_request('组名称不能为空且不超过 50 字')
    mode = body.get('quizMode', 'group')
    if mode not in QUIZ_MODES:
        raise bad_request('quizMode 仅支持 group / fallback / mixed')
    group_id = gen_id('g')
    ts = now_iso()
    execute(
        'INSERT INTO groups (id, name, description, quizMode, inviteCode, createdAt, updatedAt) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (group_id, name, str(body.get('description', ''))[:200], mode, gen_group_invite_code(), ts, ts),
    )
    execute(
        "INSERT INTO group_members (id, groupId, userId, role, joinedAt) VALUES (?, ?, ?, 'teacher', ?)",
        (gen_id('gm'), group_id, g.user['id'], ts),
    )
    commit()
    return jsonify(group_view(get_group_or_404(group_id))), 201


@bp.patch('/groups/<group_id>')
@require_auth
def update_group(group_id):
    require_teacher()
    group = get_group_or_404(group_id)
    require_manager(group_id)
    body = request.get_json(silent=True) or {}
    name = str(body.get('name', group['name'])).strip()
    if not name or len(name) > 50:
        raise bad_request('组名称不能为空且不超过 50 字')
    mode = body.get('quizMode', group['quizMode'])
    if mode not in QUIZ_MODES:
        raise bad_request('quizMode 仅支持 group / fallback / mixed')
    execute(
        'UPDATE groups SET name = ?, description = ?, quizMode = ?, updatedAt = ? WHERE id = ?',
        (name, str(body.get('description', group['description']))[:200], mode, now_iso(), group_id),
    )
    commit()
    return jsonify(group_view(get_group_or_404(group_id)))


@bp.delete('/groups/<group_id>')
@require_auth
def delete_group(group_id):
    require_teacher()
    get_group_or_404(group_id)
    require_manager(group_id)
    execute('DELETE FROM quiz_questions WHERE groupId = ?', (group_id,))
    execute('DELETE FROM group_members WHERE groupId = ?', (group_id,))
    execute('DELETE FROM groups WHERE id = ?', (group_id,))
    commit()
    return '', 204


# ---------------------------------------------------------------- 成员管理

@bp.get('/groups/<group_id>/members')
@require_auth
def list_members(group_id):
    require_teacher()
    get_group_or_404(group_id)
    require_manager(group_id)
    rows = query_all(
        'SELECT gm.id, gm.groupId, gm.userId, gm.role, gm.joinedAt, u.name, u.username '
        'FROM group_members gm JOIN users u ON u.id = gm.userId WHERE gm.groupId = ? '
        'ORDER BY gm.role DESC, gm.joinedAt',
        (group_id,),
    )
    return jsonify({'items': rows, 'total': len(rows)})


@bp.post('/groups/<group_id>/members')
@require_auth
def add_member(group_id):
    require_teacher()
    get_group_or_404(group_id)
    require_manager(group_id)
    body = request.get_json(silent=True) or {}
    user_id = body.get('userId')
    role = body.get('role', 'member')
    if not isinstance(user_id, str) or role not in ('teacher', 'member'):
        raise bad_request('userId 与 role（teacher/member）不能为空')
    if not query_one('SELECT 1 FROM users WHERE id = ?', (user_id,)):
        raise not_found('用户不存在')
    if query_one('SELECT 1 FROM group_members WHERE groupId = ? AND userId = ?', (group_id, user_id)):
        raise ApiError(409, 'ALREADY_MEMBER', '该用户已在组内')
    execute(
        'INSERT INTO group_members (id, groupId, userId, role, joinedAt) VALUES (?, ?, ?, ?, ?)',
        (gen_id('gm'), group_id, user_id, role, now_iso()),
    )
    commit()
    row = query_one(
        'SELECT gm.id, gm.groupId, gm.userId, gm.role, gm.joinedAt, u.name, u.username '
        'FROM group_members gm JOIN users u ON u.id = gm.userId WHERE gm.groupId = ? AND gm.userId = ?',
        (group_id, user_id),
    )
    return jsonify(row), 201


@bp.delete('/groups/<group_id>/members/<user_id>')
@require_auth
def remove_member(group_id, user_id):
    require_teacher()
    get_group_or_404(group_id)
    require_manager(group_id)
    row = query_one(
        'SELECT role FROM group_members WHERE groupId = ? AND userId = ?',
        (group_id, user_id),
    )
    if not row:
        raise not_found('该成员不在组内')
    if row['role'] == 'teacher':
        count = query_one(
            "SELECT COUNT(*) AS c FROM group_members WHERE groupId = ? AND role = 'teacher'",
            (group_id,),
        )['c']
        if count <= 1:
            raise bad_request('组内至少需要一名负责老师')
    execute('DELETE FROM group_members WHERE groupId = ? AND userId = ?', (group_id, user_id))
    commit()
    return '', 204


# ---------------------------------------------------------------- 题库管理

@bp.get('/groups/<group_id>/questions')
@require_auth
def list_questions(group_id):
    require_teacher()
    get_group_or_404(group_id)
    require_manager(group_id)
    rows = query_all(
        'SELECT * FROM quiz_questions WHERE groupId = ? ORDER BY updatedAt DESC, id',
        (group_id,),
    )
    return jsonify({'items': [question_view(r) for r in rows], 'total': len(rows)})


@bp.post('/groups/<group_id>/questions')
@require_auth
def create_question(group_id):
    require_teacher()
    get_group_or_404(group_id)
    require_manager(group_id)
    data = validate_question(request.get_json(silent=True) or {})
    qid = gen_id('q')
    ts = now_iso()
    execute(
        'INSERT INTO quiz_questions (id, groupId, createdBy, createdAt, updatedAt, category, difficulty, '
        'question, options, answer, explanation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (qid, group_id, g.user['id'], ts, ts, data['category'], data['difficulty'], data['question'],
         json_dumps(data['options']), data['answer'], data['explanation']),
    )
    commit()
    return jsonify(question_view(query_one('SELECT * FROM quiz_questions WHERE id = ?', (qid,)))), 201


@bp.patch('/groups/<group_id>/questions/<question_id>')
@require_auth
def update_question(group_id, question_id):
    require_teacher()
    get_group_or_404(group_id)
    require_manager(group_id)
    if not query_one('SELECT 1 FROM quiz_questions WHERE id = ? AND groupId = ?', (question_id, group_id)):
        raise not_found('题目不存在')
    data = validate_question(request.get_json(silent=True) or {})
    execute(
        'UPDATE quiz_questions SET category = ?, difficulty = ?, question = ?, options = ?, answer = ?, '
        'explanation = ?, updatedAt = ? WHERE id = ?',
        (data['category'], data['difficulty'], data['question'], json_dumps(data['options']), data['answer'],
         data['explanation'], now_iso(), question_id),
    )
    commit()
    return jsonify(question_view(query_one('SELECT * FROM quiz_questions WHERE id = ?', (question_id,))))


@bp.delete('/groups/<group_id>/questions/<question_id>')
@require_auth
def delete_question(group_id, question_id):
    require_teacher()
    get_group_or_404(group_id)
    require_manager(group_id)
    if not query_one('SELECT 1 FROM quiz_questions WHERE id = ? AND groupId = ?', (question_id, group_id)):
        raise not_found('题目不存在')
    execute('DELETE FROM quiz_questions WHERE id = ?', (question_id,))
    commit()
    return '', 204


# ---------------------------------------------------------------- 邀请

@bp.post('/groups/join')
@require_auth
def join_group():
    """学生凭邀请码直接入组（契约 3.11）。"""
    if g.user['role'] != 'student':
        raise forbidden('仅学生可通过邀请码加入分组')
    body = request.get_json(silent=True) or {}
    code = str(body.get('inviteCode') or '').strip()
    group = query_one('SELECT * FROM groups WHERE inviteCode = ? COLLATE NOCASE', (code,))
    if not group:
        raise ApiError(409, 'INVALID_INVITE', '邀请码无效')
    if query_one('SELECT 1 FROM group_members WHERE groupId = ? AND userId = ?', (group['id'], g.user['id'])):
        raise ApiError(409, 'ALREADY_MEMBER', '你已在该分组中')
    execute(
        'INSERT INTO group_members (id, groupId, userId, role, joinedAt) VALUES (?, ?, ?, ?, ?)',
        (gen_id('gm'), group['id'], g.user['id'], 'member', now_iso()),
    )
    commit()
    return jsonify(group_view(get_group_or_404(group['id']))), 201


@bp.post('/groups/<group_id>/invites')
@require_auth
def send_invite(group_id):
    """负责老师给学生发送入组邀请（契约 3.11）。"""
    require_teacher()
    get_group_or_404(group_id)
    require_manager(group_id)
    body = request.get_json(silent=True) or {}
    user_id = body.get('userId')
    if not isinstance(user_id, str):
        raise bad_request('userId 不能为空')
    user = query_one('SELECT id, role FROM users WHERE id = ?', (user_id,))
    if not user:
        raise not_found('用户不存在')
    if user['role'] != 'student':
        raise bad_request('负责老师请直接添加，邀请仅面向学生')
    if query_one('SELECT 1 FROM group_members WHERE groupId = ? AND userId = ?', (group_id, user_id)):
        raise ApiError(409, 'ALREADY_MEMBER', '该用户已在组内')
    if query_one(
        "SELECT 1 FROM group_invites WHERE groupId = ? AND userId = ? AND status = 'pending'",
        (group_id, user_id),
    ):
        raise ApiError(409, 'ALREADY_INVITED', '已发送过邀请，等待学生确认')
    invite = {
        'id': gen_id('gi'),
        'groupId': group_id,
        'userId': user_id,
        'inviterId': g.user['id'],
        'status': 'pending',
        'createdAt': now_iso(),
        'respondedAt': None,
    }
    execute(
        'INSERT INTO group_invites (id, groupId, userId, inviterId, status, createdAt, respondedAt) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        tuple(invite.values()),
    )
    commit()
    return jsonify(invite), 201


@bp.get('/groups/<group_id>/invites')
@require_auth
def list_invites(group_id):
    """负责老师查看组内待处理邀请。"""
    require_teacher()
    get_group_or_404(group_id)
    require_manager(group_id)
    rows = query_all(
        'SELECT gi.id, gi.groupId, gi.userId, gi.inviterId, gi.status, gi.createdAt, u.name, u.username '
        'FROM group_invites gi JOIN users u ON u.id = gi.userId '
        "WHERE gi.groupId = ? AND gi.status = 'pending' ORDER BY gi.createdAt",
        (group_id,),
    )
    return jsonify({'items': rows, 'total': len(rows)})


@bp.delete('/groups/<group_id>/invites/<invite_id>')
@require_auth
def withdraw_invite(group_id, invite_id):
    """负责老师撤回待处理邀请。"""
    require_teacher()
    get_group_or_404(group_id)
    require_manager(group_id)
    row = query_one(
        "SELECT id FROM group_invites WHERE id = ? AND groupId = ? AND status = 'pending'",
        (invite_id, group_id),
    )
    if not row:
        raise not_found('邀请不存在或已处理')
    execute('DELETE FROM group_invites WHERE id = ?', (invite_id,))
    commit()
    return '', 204


@bp.get('/groups/invites')
@require_auth
def my_invites():
    """学生：我的待处理入组邀请（含组名与邀请老师）。"""
    rows = query_all(
        'SELECT gi.id, gi.groupId, gi.status, gi.createdAt, g.name AS groupName, u.name AS inviterName '
        'FROM group_invites gi JOIN groups g ON g.id = gi.groupId JOIN users u ON u.id = gi.inviterId '
        "WHERE gi.userId = ? AND gi.status = 'pending' ORDER BY gi.createdAt",
        (g.user['id'],),
    )
    return jsonify({'items': rows, 'total': len(rows)})


@bp.post('/groups/invites/<invite_id>/respond')
@require_auth
def respond_invite(invite_id):
    """学生：通过（入组）或拒绝邀请。"""
    body = request.get_json(silent=True) or {}
    accept = body.get('accept')
    if not isinstance(accept, bool):
        raise bad_request('accept 需为布尔值')
    invite = query_one(
        "SELECT * FROM group_invites WHERE id = ? AND userId = ? AND status = 'pending'",
        (invite_id, g.user['id']),
    )
    if not invite:
        raise not_found('邀请不存在或已处理')
    if accept:
        if not query_one('SELECT 1 FROM group_members WHERE groupId = ? AND userId = ?',
                         (invite['groupId'], invite['userId'])):
            execute(
                'INSERT INTO group_members (id, groupId, userId, role, joinedAt) VALUES (?, ?, ?, ?, ?)',
                (gen_id('gm'), invite['groupId'], invite['userId'], 'member', now_iso()),
            )
        execute("UPDATE group_invites SET status = 'accepted', respondedAt = ? WHERE id = ?",
                (now_iso(), invite_id))
    else:
        execute("UPDATE group_invites SET status = 'declined', respondedAt = ? WHERE id = ?",
                (now_iso(), invite_id))
    commit()
    return jsonify({'status': 'accepted' if accept else 'declined'})


# ---------------------------------------------------------------- 用户搜索

@bp.get('/users')
@require_auth
def search_users():
    """教师：按用户名/姓名搜索用户（添加成员用）。"""
    require_teacher()
    keyword = request.args.get('keyword', '').strip().lower()
    if keyword:
        rows = query_all(
            'SELECT id, username, name, role FROM users WHERE LOWER(username) LIKE ? OR LOWER(name) LIKE ? '
            'ORDER BY role, name LIMIT 20',
            (f'%{keyword}%', f'%{keyword}%'),
        )
    else:
        rows = query_all('SELECT id, username, name, role FROM users ORDER BY role, name LIMIT 20')
    return jsonify({'items': rows, 'total': len(rows)})
