"""PBL 里程碑任务路由（契约 3.4）：任务 CRUD + 状态流转 + 任务动态。"""
from flask import Blueprint, g, jsonify, request

from ..auth import require_auth
from ..db import commit, execute, gen_id, now_iso, query_all, query_one
from ..errors import bad_request, forbidden, get_json_body
from ..services import (
    TASK_STATUSES,
    add_task_log,
    ensure_read_access,
    get_project_or_404,
    get_task_or_404,
    handle_task_status_change,
    member_of,
    paged,
    touch_project,
)

bp = Blueprint('tasks', __name__)


@bp.get('/projects/<project_id>/tasks')
@require_auth
def list_tasks(project_id):
    """任务列表，支持 ?status= 与 ?assigneeId= 过滤（契约 3.4）。"""
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    items = query_all('SELECT * FROM tasks WHERE projectId = ?', (project['id'],))
    status = request.args.get('status')
    assignee_id = request.args.get('assigneeId')
    if status:
        items = [t for t in items if t['status'] == status]
    if assignee_id:
        items = [t for t in items if t['assigneeId'] == assignee_id]
    return jsonify(paged(items))


@bp.post('/projects/<project_id>/tasks')
@require_auth
def create_task(project_id):
    project = get_project_or_404(project_id)
    member_of(project['id'], g.user)
    body = get_json_body()
    title = str(body.get('title') or '').strip()
    if not title:
        raise bad_request('任务标题不能为空')
    now = now_iso()
    task = {
        'id': gen_id('t'),
        'projectId': project['id'],
        'title': title,
        'description': str(body.get('description') or ''),
        'assigneeId': None,
        'status': 'todo',
        'dueDate': body.get('dueDate') or None,
        'createdAt': now,
        'updatedAt': now,
    }
    execute('INSERT INTO tasks (id, projectId, title, description, assigneeId, status, dueDate, createdAt, updatedAt) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            tuple(task.values()))
    add_task_log(project['id'], task['id'], g.user, 'create', '创建任务')
    touch_project(project['id'])
    commit()
    return jsonify(task), 201


@bp.patch('/tasks/<task_id>')
@require_auth
def update_task(task_id):
    """编辑 / 认领 / 状态流转（契约 3.4）。"""
    task = get_task_or_404(task_id)
    project = get_project_or_404(task['projectId'])
    member_of(project['id'], g.user)
    body = get_json_body()
    old_status = task['status']

    if 'title' in body:
        title = str(body['title'] or '').strip()
        if not title:
            raise bad_request('任务标题不能为空')
        execute('UPDATE tasks SET title = ? WHERE id = ?', (title, task['id']))
        add_task_log(project['id'], task['id'], g.user, 'edit', '修改任务信息')
    if 'description' in body:
        execute('UPDATE tasks SET description = ? WHERE id = ?', (str(body['description'] or ''), task['id']))
        add_task_log(project['id'], task['id'], g.user, 'edit', '修改任务描述')
    if 'dueDate' in body:
        execute('UPDATE tasks SET dueDate = ? WHERE id = ?', (body['dueDate'] or None, task['id']))
        add_task_log(project['id'], task['id'], g.user, 'edit', '修改截止日期')
    if 'assigneeId' in body:
        value = body['assigneeId']
        if value is not None and value != g.user['id']:
            raise forbidden('只能认领给自己')
        execute('UPDATE tasks SET assigneeId = ? WHERE id = ?', (value, task['id']))
        add_task_log(project['id'], task['id'], g.user, 'claim', '认领任务' if value else '取消认领')
    if 'status' in body:
        value = body['status']
        if value not in TASK_STATUSES:
            raise bad_request('无效的任务状态')
        execute('UPDATE tasks SET status = ? WHERE id = ?', (value, task['id']))
        task['status'] = value
        handle_task_status_change(task, g.user, old_status)

    execute('UPDATE tasks SET updatedAt = ? WHERE id = ?', (now_iso(), task['id']))
    touch_project(project['id'])
    commit()
    return jsonify(get_task_or_404(task_id))


@bp.delete('/tasks/<task_id>')
@require_auth
def delete_task(task_id):
    task = get_task_or_404(task_id)
    project = get_project_or_404(task['projectId'])
    member_of(project['id'], g.user)
    execute('DELETE FROM tasks WHERE id = ?', (task['id'],))
    add_task_log(project['id'], task['id'], g.user, 'delete', f"删除任务「{task['title']}」")
    touch_project(project['id'])
    commit()
    return '', 204


@bp.get('/projects/<project_id>/task-logs')
@require_auth
def list_task_logs(project_id):
    """任务动态（版本记录），按时间倒序（契约 3.4）。"""
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    items = query_all('SELECT * FROM task_logs WHERE projectId = ? ORDER BY createdAt DESC', (project['id'],))
    return jsonify(paged(items))
