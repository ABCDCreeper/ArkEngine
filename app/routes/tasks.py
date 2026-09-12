"""PBL 里程碑任务路由（契约 3.4）：任务 CRUD + 状态流转 + 任务动态。

状态机：待认领 → 进行中 → 待验收 → 已完成。学生可推进前三步并可撤回修改；
只有负责教师能验收（review → done）或退回（review → doing）。待验收期间内容冻结。
"""
from flask import Blueprint, g, jsonify, request

from ..auth import require_auth
from ..db import commit, execute, gen_id, now_iso, query_one
from ..errors import ApiError, bad_request, forbidden, get_json_body
from ..validation import criteria_value, due_date, text_value
from ..services import (
    TASK_STATUSES,
    begin_write,
    can_review,
    ensure_active,
    add_task_log,
    ensure_read_access,
    get_project_or_404,
    get_task_or_404,
    handle_task_status_change,
    member_of,
    paged_query,
    task_view,
    touch_project,
)

bp = Blueprint('tasks', __name__)


@bp.get('/projects/<project_id>/tasks')
@require_auth
def list_tasks(project_id):
    """任务列表，支持 ?status= 与 ?assigneeId= 过滤（契约 3.4），分页在 SQL 层完成。"""
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    # join 出验收人姓名（教师不属于项目成员表），并附上任务维度的过程证据：
    # 关联的专注时长与同伴互评数——这两项都是真实记录，不靠推断
    sql = ('SELECT t.*, u.name AS verifiedByName, '
           "COALESCE((SELECT SUM(f.durationMin) FROM focus_sessions f "
           "WHERE f.taskId = t.id AND f.type = 'focus'), 0) AS focusMinutes, "
           '(SELECT COUNT(*) FROM task_reviews r WHERE r.taskId = t.id) AS peerReviewCount '
           'FROM tasks t LEFT JOIN users u ON u.id = t.verifiedBy WHERE t.projectId = ?')
    args: list = [project['id']]
    status = request.args.get('status')
    assignee_id = request.args.get('assigneeId')
    if status:
        sql += ' AND t.status = ?'
        args.append(status)
    if assignee_id:
        sql += ' AND t.assigneeId = ?'
        args.append(assignee_id)
    sql += ' ORDER BY t.createdAt'
    return jsonify(paged_query(sql, args))


@bp.post('/projects/<project_id>/tasks')
@require_auth
def create_task(project_id):
    project = get_project_or_404(project_id)
    member_of(project['id'], g.user)
    body = get_json_body()
    title = text_value(body.get('title'), '任务标题', 200, 1)
    if not title:
        raise bad_request('任务标题不能为空')
    now = now_iso()
    task = {
        'id': gen_id('t'),
        'projectId': project['id'],
        'title': title,
        'description': text_value(body.get('description', ''), '任务描述', 5000, strip=False),
        'assigneeId': None,
        'status': 'todo',
        'dueDate': due_date(body.get('dueDate')),
        'createdAt': now,
        'updatedAt': now,
        'statusChangedAt': now,
        'sourceAnnotationId': None,
        'criteria': criteria_value(body.get('criteria')),
        'verifiedBy': None,
        'verifiedAt': None,
    }
    execute('INSERT INTO tasks (id, projectId, title, description, assigneeId, status, dueDate, '
            'createdAt, updatedAt, statusChangedAt, sourceAnnotationId, criteria, verifiedBy, verifiedAt) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            tuple(task.values()))
    add_task_log(project['id'], task['id'], g.user, 'create', '创建任务')
    touch_project(project['id'])
    commit()
    return jsonify(task_view(task)), 201


@bp.patch('/tasks/<task_id>')
@require_auth
def update_task(task_id):
    """编辑 / 认领 / 状态流转（契约 3.4）。"""
    begin_write()
    task = get_task_or_404(task_id)
    project = get_project_or_404(task['projectId'])
    body = get_json_body()
    allowed_fields = {'title', 'description', 'dueDate', 'assigneeId', 'status', 'criteria'}
    if not body or set(body) - allowed_fields:
        raise bad_request('请提供有效的任务修改字段')
    if 'title' in body:
        body['title'] = text_value(body['title'], '任务标题', 200, 1)
    if 'description' in body:
        body['description'] = text_value(body['description'], '任务描述', 5000, strip=False)
    if 'dueDate' in body:
        body['dueDate'] = due_date(body['dueDate'])
    if 'criteria' in body:
        body['criteria'] = criteria_value(body['criteria'])
    ensure_active(project['id'])
    reviewer = can_review(project, g.user)
    if reviewer:
        # 教师验收时可一并提交逐条核对结果（criteria 带 done 标记）
        if set(body) - {'status', 'criteria'}:
            raise forbidden('教师仅可验收或退回任务')
    else:
        member_of(project['id'], g.user)
    old_status = task['status']
    value = body.get('status', old_status)
    if not isinstance(value, str) or value not in TASK_STATUSES:
        raise bad_request('无效的任务状态')
    if old_status == 'done':
        if body == {'status': 'done'} and reviewer:
            commit()
            return jsonify(task_view(task))
        raise ApiError(409, 'TASK_FINISHED', '已验收任务只读')
    if not reviewer and task['assigneeId'] not in (None, g.user['id']):
        raise forbidden('仅任务认领人可修改任务')
    # 待验收期间内容冻结：否则教师正在验收的内容可能被改写，验收结论对不上提交物。
    # 需要修改就先撤回（review -> doing），撤回会重置验收计时并留下动态记录。
    if not reviewer and old_status == 'review' and {'title', 'description', 'dueDate', 'criteria'} & set(body):
        raise ApiError(409, 'TASK_IN_REVIEW', '任务已提交验收，请先撤回修改后再编辑')
    if 'assigneeId' in body:
        if old_status != 'todo':
            raise ApiError(409, 'TASK_IN_PROGRESS', '仅待认领任务可变更负责人')
        if body['assigneeId'] not in (None, g.user['id']):
            raise forbidden('只能认领给自己')
    assignee = body.get('assigneeId', task['assigneeId'])
    if value != old_status:
        student_moves = {('todo', 'doing'), ('doing', 'review'), ('review', 'doing')}
        allowed = {('review', 'done'), ('review', 'doing')} if reviewer else student_moves
        if (old_status, value) not in allowed:
            raise ApiError(409, 'INVALID_TRANSITION', '请按认领、进行中、待验收、教师验收的顺序推进')
        if not assignee:
            raise ApiError(409, 'ASSIGNEE_REQUIRED', '请先认领任务')
    body = {key: val for key, val in body.items() if val != task[key]}
    if not body:
        # 没有任何实际变化：不写库也不产生动态，响应形状与正常路径保持一致
        commit()
        return jsonify(task_view(task))
    task['assigneeId'] = assignee

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
    if 'criteria' in body:
        execute('UPDATE tasks SET criteria = ? WHERE id = ?', (body['criteria'], task['id']))
        task['criteria'] = body['criteria']  # 后续状态流转的日志要读最新清单
        add_task_log(project['id'], task['id'], g.user, 'edit', '更新验收标准')
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
        # 验收通过时记录验收人与时间：任务动态与批注线程据此给出回执
        if value == 'done' and old_status != 'done':
            execute('UPDATE tasks SET status = ?, statusChangedAt = ?, verifiedBy = ?, verifiedAt = ? '
                    'WHERE id = ?', (value, now_iso(), g.user['id'], now_iso(), task['id']))
        else:
            execute('UPDATE tasks SET status = ?, statusChangedAt = ? WHERE id = ?',
                    (value, now_iso(), task['id']))
        task['status'] = value
        handle_task_status_change(task, g.user, old_status)

    execute('UPDATE tasks SET updatedAt = ? WHERE id = ?', (now_iso(), task['id']))
    touch_project(project['id'])
    commit()
    return jsonify(task_view(get_task_or_404(task_id)))


@bp.post('/projects/<project_id>/tasks/batch-verify')
@require_auth
def batch_verify_tasks(project_id):
    """批量验收（仅负责教师）：一次通过多项待验收任务。

    逐项返回结果而不是整体失败：某一项状态已变（学生撤回、别人已验收）时只跳过该项，
    其余仍然生效，避免教师反复点。
    """
    begin_write()
    project = get_project_or_404(project_id)
    if not can_review(project, g.user):
        raise forbidden('仅负责教师可验收任务')
    ensure_active(project_id)
    body = get_json_body()
    task_ids = body.get('taskIds')
    if not isinstance(task_ids, list) or not task_ids:
        raise bad_request('请提供要验收的任务')
    if len(task_ids) > 50:
        raise bad_request('一次最多验收 50 项任务')
    now = now_iso()
    results = []
    for task_id in task_ids:
        task = query_one('SELECT * FROM tasks WHERE id = ? AND projectId = ?', (task_id, project_id))
        if not task:
            results.append({'id': task_id, 'status': 'not_found'})
            continue
        if task['status'] != 'review':
            results.append({'id': task_id, 'title': task['title'], 'status': 'skipped',
                            'reason': '仅待验收任务可批量验收'})
            continue
        execute('UPDATE tasks SET status = ?, statusChangedAt = ?, verifiedBy = ?, verifiedAt = ?, '
                'updatedAt = ? WHERE id = ?', ('done', now, g.user['id'], now, now, task_id))
        handle_task_status_change(dict(task, status='done'), g.user, task['status'])
        results.append({'id': task_id, 'title': task['title'], 'status': 'verified'})
    touch_project(project_id)
    commit()
    return jsonify({
        'results': results,
        'verified': sum(1 for r in results if r['status'] == 'verified'),
    })


@bp.delete('/tasks/<task_id>')
@require_auth
def delete_task(task_id):
    begin_write()
    task = get_task_or_404(task_id)
    project = get_project_or_404(task['projectId'])
    member_of(project['id'], g.user)
    if task['status'] != 'todo':
        raise ApiError(409, 'TASK_IN_PROGRESS', '仅待认领任务可删除')
    if task['assigneeId'] not in (None, g.user['id']):
        raise forbidden('仅任务认领人可删除任务')
    execute('DELETE FROM tasks WHERE id = ?', (task['id'],))
    add_task_log(project['id'], task['id'], g.user, 'delete', f"删除任务「{task['title']}」")
    touch_project(project['id'])
    commit()
    return '', 204


@bp.get('/projects/<project_id>/task-logs')
@require_auth
def list_task_logs(project_id):
    """任务动态（版本记录），按时间倒序（契约 3.4），分页在 SQL 层完成。"""
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    return jsonify(paged_query(
        'SELECT l.*, u.name AS userName FROM task_logs l LEFT JOIN users u ON u.id = l.userId '
        'WHERE l.projectId = ? ORDER BY l.createdAt DESC', [project_id]))
