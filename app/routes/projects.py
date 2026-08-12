"""课题与项目路由（契约 3.2）：课题库、项目 CRUD、邀请码组队。"""
import secrets
import string

from flask import Blueprint, g, jsonify

from ..auth import require_auth
from ..db import commit, execute, gen_id, now_iso, query_all, query_one
from ..errors import ApiError, bad_request, get_json_body
from ..services import (
    add_feedback,
    ensure_read_access,
    get_project_or_404,
    member_of,
    paged,
    project_view,
    topic_view,
    touch_project,
)

bp = Blueprint('projects', __name__)


def gen_invite_code() -> str:
    """生成全局唯一邀请码（如 P-7F3A 风格）。"""
    while True:
        code = 'P' + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
        if not query_one('SELECT 1 FROM projects WHERE inviteCode = ?', (code,)):
            return code


@bp.get('/topics')
@require_auth
def list_topics():
    return jsonify(paged([topic_view(t) for t in query_all('SELECT * FROM topics')]))


@bp.get('/projects')
@require_auth
def list_projects():
    """当前用户参与的项目（教师无成员关系，返回空数组，契约 3.2）。"""
    rows = query_all(
        'SELECT p.* FROM members m JOIN projects p ON p.id = m.projectId WHERE m.userId = ?',
        (g.user['id'],),
    )
    return jsonify(paged([project_view(p) for p in rows]))


@bp.post('/projects')
@require_auth
def create_project():
    """发起项目：自动成为组长并加入成员、生成邀请码、初始化根导图节点。"""
    body = get_json_body()
    topic = query_one('SELECT * FROM topics WHERE id = ?', (body.get('topicId'),))
    if not topic:
        raise bad_request('课题不存在')
    now = now_iso()
    project = {
        'id': gen_id('p'),
        'topicId': topic['id'],
        'name': body.get('name') or topic['title'],
        'status': 'active',
        'inviteCode': gen_invite_code(),
        'leaderId': g.user['id'],
        'createdAt': now,
        'updatedAt': now,
        'finishedAt': None,
    }
    execute(
        'INSERT INTO projects (id, topicId, name, status, inviteCode, leaderId, createdAt, updatedAt, finishedAt) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        tuple(project.values()),
    )
    execute('INSERT INTO members (id, projectId, userId, joinedAt) VALUES (?, ?, ?, ?)',
            (gen_id('m'), project['id'], g.user['id'], now))
    execute('INSERT INTO mind_nodes (id, projectId, parentId, label, createdAt, updatedAt) VALUES (?, ?, ?, ?, ?, ?)',
            (gen_id('n'), project['id'], None, project['name'], now, now))
    commit()
    return jsonify(project_view(get_project_or_404(project['id']))), 201


@bp.post('/projects/join')
@require_auth
def join_project():
    """邀请码加入：无效 / 已加入 / 满员分别返回 409（契约 3.2）。"""
    body = get_json_body()
    code = str(body.get('inviteCode') or '').strip()
    project = query_one('SELECT * FROM projects WHERE inviteCode = ? COLLATE NOCASE', (code,))
    if not project:
        raise ApiError(409, 'INVALID_INVITE', '邀请码无效')
    if query_one('SELECT 1 FROM members WHERE projectId = ? AND userId = ?', (project['id'], g.user['id'])):
        raise ApiError(409, 'ALREADY_MEMBER', '你已在该项目中')
    count = query_one('SELECT COUNT(*) AS c FROM members WHERE projectId = ?', (project['id'],))['c']
    if count >= 4:
        raise ApiError(409, 'TEAM_FULL', '队伍已满（最多 4 人）')
    execute('INSERT INTO members (id, projectId, userId, joinedAt) VALUES (?, ?, ?, ?)',
            (gen_id('m'), project['id'], g.user['id'], now_iso()))
    commit()
    return jsonify(project_view(get_project_or_404(project['id']))), 201


@bp.get('/projects/<project_id>')
@require_auth
def project_detail(project_id):
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    return jsonify(project_view(project))


@bp.patch('/projects/<project_id>')
@require_auth
def update_project(project_id):
    """更新项目名/简介；status=finished 结题（记录 finishedAt 并生成里程碑反馈）。组员与教师均可填写简介。"""
    project = get_project_or_404(project_id)
    if g.user['role'] != 'teacher':
        member_of(project['id'], g.user)
    body = get_json_body()
    if 'name' in body:
        if not body['name']:
            raise bad_request('项目名称不能为空')
        execute('UPDATE projects SET name = ? WHERE id = ?', (body['name'], project['id']))
    if 'description' in body:
        description = body['description']
        if not isinstance(description, str):
            raise bad_request('简介格式不正确')
        if len(description) > 2000:
            raise bad_request('简介不能超过 2000 字')
        execute('UPDATE projects SET description = ? WHERE id = ?', (description, project['id']))
    if body.get('status') == 'finished':
        execute('UPDATE projects SET status = ?, finishedAt = ? WHERE id = ?',
                ('finished', now_iso(), project['id']))
        add_feedback(project['id'], g.user, 'milestone', '项目已结题！系统已自动整合全部过程记录，生成科创档案。')
    touch_project(project['id'])
    commit()
    return jsonify(project_view(get_project_or_404(project_id)))
