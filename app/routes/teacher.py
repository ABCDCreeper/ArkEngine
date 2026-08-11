"""教师端与成果归档路由（契约 3.8 / 3.9）：团队总览、批注、科创档案。"""
from datetime import datetime

from flask import Blueprint, g, jsonify

from ..auth import require_auth
from ..db import commit, execute, gen_id, now_iso, query_all
from ..errors import ApiError, bad_request, forbidden, get_json_body
from ..services import (
    ensure_read_access,
    get_project_or_404,
    paged,
    project_members,
    project_view,
)

bp = Blueprint('teacher', __name__)


@bp.get('/teacher/projects')
@require_auth
def teacher_projects():
    """全部团队总览（仅教师），按最近更新倒序（契约 3.8）。"""
    if g.user['role'] != 'teacher':
        raise forbidden('仅教师可访问')
    rows = query_all('SELECT * FROM projects ORDER BY updatedAt DESC')
    return jsonify(paged([project_view(p) for p in rows]))


@bp.get('/projects/<project_id>/annotations')
@require_auth
def list_annotations(project_id):
    """批注列表：学生（成员）只读、教师可读（契约 3.8）。"""
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    items = query_all('SELECT * FROM annotations WHERE projectId = ? ORDER BY createdAt ASC', (project['id'],))
    return jsonify(paged(items))


@bp.post('/projects/<project_id>/annotations')
@require_auth
def create_annotation(project_id):
    """添加批注（仅教师，契约 3.8）。"""
    if g.user['role'] != 'teacher':
        raise forbidden('仅教师可添加批注')
    project = get_project_or_404(project_id)
    body = get_json_body()
    content = str(body.get('content') or '').strip()
    if not content:
        raise bad_request('批注内容不能为空')
    annotation = {'id': gen_id('a'), 'projectId': project['id'], 'userId': g.user['id'],
                  'content': content, 'createdAt': now_iso()}
    execute('INSERT INTO annotations (id, projectId, userId, content, createdAt) VALUES (?, ?, ?, ?, ?)',
            tuple(annotation.values()))
    commit()
    return jsonify(annotation), 201


@bp.get('/projects/<project_id>/archive')
@require_auth
def get_archive(project_id):
    """科创档案（派生资源，仅已结题项目，契约 2.13 / 3.9）。"""
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    if project['status'] != 'finished':
        raise ApiError(409, 'PROJECT_NOT_FINISHED', '项目结题后即可生成科创档案')

    members = []
    for u in project_members(project['id']):
        mine = query_all('SELECT * FROM tasks WHERE projectId = ? AND assigneeId = ?',
                         (project['id'], u['id']))
        checkins = query_all('SELECT * FROM checkins WHERE projectId = ? AND userId = ?',
                             (project['id'], u['id']))
        members.append({
            'user': u,
            'taskCount': len(mine),
            'doneCount': sum(1 for t in mine if t['status'] == 'done'),
            'checkinCount': len(checkins),
        })

    def parse(iso_str: str) -> datetime:
        return datetime.fromisoformat(iso_str.replace('Z', '+00:00'))

    duration_days = round((parse(project['finishedAt']) - parse(project['createdAt'])).total_seconds() / 86400)

    tasks = query_all('SELECT * FROM tasks WHERE projectId = ?', (project['id'],))
    return jsonify({
        'project': project_view(project),
        'summary': {
            'taskTotal': len(tasks),
            'doneTotal': sum(1 for t in tasks if t['status'] == 'done'),
            'checkinTotal': len(query_all('SELECT 1 FROM checkins WHERE projectId = ?', (project['id'],))),
            'feedbackTotal': len(query_all('SELECT 1 FROM feedbacks WHERE projectId = ?', (project['id'],))),
            'durationDays': duration_days,
        },
        'members': members,
        'tasks': tasks,
        'checkins': query_all('SELECT * FROM checkins WHERE projectId = ?', (project['id'],)),
        'feedbacks': query_all('SELECT * FROM feedbacks WHERE projectId = ?', (project['id'],)),
        'mindNodes': query_all('SELECT * FROM mind_nodes WHERE projectId = ?', (project['id'],)),
        'annotations': query_all('SELECT * FROM annotations WHERE projectId = ?', (project['id'],)),
    })
