"""打卡与动态反馈路由（契约 3.5）：打卡、系统反馈列表。"""
from flask import Blueprint, g, jsonify

from ..auth import require_auth
from ..db import commit, execute, gen_id, now_iso, query_all
from ..errors import bad_request, get_json_body
from ..services import (
    add_feedback,
    ensure_read_access,
    get_project_or_404,
    member_of,
    paged,
    pick_feedback,
    touch_project,
)

bp = Blueprint('activity', __name__)


@bp.get('/projects/<project_id>/checkins')
@require_auth
def list_checkins(project_id):
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    items = query_all('SELECT * FROM checkins WHERE projectId = ? ORDER BY createdAt DESC', (project['id'],))
    return jsonify(paged(items))


@bp.post('/projects/<project_id>/checkins')
@require_auth
def create_checkin(project_id):
    """打卡成功同时生成一条 guide 类型系统反馈（契约 3.5）。"""
    project = get_project_or_404(project_id)
    member_of(project['id'], g.user)
    body = get_json_body()
    content = str(body.get('content') or '').strip()
    if not content:
        raise bad_request('打卡内容不能为空')
    checkin = {'id': gen_id('c'), 'projectId': project['id'], 'userId': g.user['id'],
               'content': content, 'createdAt': now_iso()}
    execute('INSERT INTO checkins (id, projectId, userId, content, createdAt) VALUES (?, ?, ?, ?, ?)',
            tuple(checkin.values()))
    add_feedback(project['id'], g.user, 'guide', pick_feedback())
    touch_project(project['id'])
    commit()
    return jsonify(checkin), 201


@bp.get('/projects/<project_id>/feedbacks')
@require_auth
def list_feedbacks(project_id):
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    items = query_all('SELECT * FROM feedbacks WHERE projectId = ? ORDER BY createdAt DESC', (project['id'],))
    return jsonify(paged(items))
