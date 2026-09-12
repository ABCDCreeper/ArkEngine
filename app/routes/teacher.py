"""教师端与成果归档路由（契约 3.8 / 3.9）：团队总览、批注、科创档案。"""
from datetime import datetime

from flask import Blueprint, current_app, g, jsonify, request

from ..auth import require_auth
from ..db import commit, execute, gen_id, json_loads, now_iso, query_all, query_one
from ..errors import ApiError, bad_request, forbidden, get_json_body, not_found
from ..validation import criteria_value, due_date, optional_text
from ..routes.review import evaluation_view
from ..services import (
    add_task_log,
    is_teacher_tier,
    can_manage_group,
    can_review,
    ensure_active,
    ensure_read_access,
    get_project_or_404,
    member_of,
    paged,
    project_members,
    project_view,
    project_views,
    school_scope,
    task_view,
    touch_project,
)

bp = Blueprint('teacher', __name__)

# 批注一律附带作者姓名，前端不应自行猜测作者
ANNOTATION_SELECT = (
    'SELECT a.id, a.projectId, a.userId, a.content, a.createdAt, u.name AS userName '
    'FROM annotations a LEFT JOIN users u ON u.id = a.userId '
)


def annotations_with_tasks(project_id: str) -> list[dict]:
    """批注列表附带由该批注派生的任务，用于展示「批注 → 任务 → 验收」的闭环。

    已验收的任务带上验收人与时间：教师由此确认自己提的问题被处理到什么程度。
    """
    items = query_all(ANNOTATION_SELECT + 'WHERE a.projectId = ? ORDER BY a.createdAt ASC', (project_id,))
    derived = query_all(
        'SELECT t.id, t.title, t.status, t.sourceAnnotationId, t.verifiedAt, u.name AS verifiedByName '
        'FROM tasks t LEFT JOIN users u ON u.id = t.verifiedBy '
        'WHERE t.projectId = ? AND t.sourceAnnotationId IS NOT NULL', (project_id,))
    by_annotation: dict[str, list[dict]] = {}
    for task in derived:
        by_annotation.setdefault(task['sourceAnnotationId'], []).append({
            'id': task['id'], 'title': task['title'], 'status': task['status'],
            'verifiedAt': task['verifiedAt'], 'verifiedByName': task['verifiedByName'],
        })
    for item in items:
        item['linkedTasks'] = by_annotation.get(item['id'], [])
    return items


def overlay_live_evaluations(payload: dict, project_id: str) -> dict:
    """把当前教师评分叠加到已结题项目的档案/评价上。

    档案里的自动指标冻结在结题时刻（不可篡改），但教师量化评分属于人工作出的判断，
    实际流程里往往在结题之后才补录。若不叠加，评了分却在档案里看不到。
    """
    from .review import evaluation_summary
    payload['evaluations'] = [
        evaluation_view(row) for row in query_all(
            'SELECT e.*, u.name AS userName, v.name AS evaluatorName FROM evaluations e '
            'LEFT JOIN users u ON u.id = e.userId LEFT JOIN users v ON v.id = e.evaluatorId '
            'WHERE e.projectId = ? ORDER BY u.name', (project_id,))
    ]
    assessment = payload.get('assessment')
    if isinstance(assessment, dict):
        assessment['evaluation'] = evaluation_summary(project_id)
    else:
        payload['evaluation'] = evaluation_summary(project_id)
    return payload


def normalize_archive(archive: dict) -> dict:
    """补齐旧快照缺失的字段。

    结题档案是不可变的历史快照，后加的字段（批注的 linkedTasks、预警的 responses）
    在旧快照里不存在，直接返回会让前端读到 undefined 而报错，因此在读取时统一补齐。
    """
    for annotation in archive.get('annotations') or []:
        annotation.setdefault('linkedTasks', [])
    assessment = archive.get('assessment') or {}
    for risk in assessment.get('risks') or []:
        risk.setdefault('responses', [])
    for member in archive.get('members') or []:
        member.setdefault('focusMinutes', 0)
    archive.setdefault('peerReviews', [])
    archive.setdefault('evaluations', [])
    assessment.setdefault('peerReview', {'total': 0, 'acknowledge': 0, 'question': 0, 'questionedTaskIds': []})
    assessment.setdefault('evaluation', {'count': 0, 'avgTotal': None, 'dimensions': {}})
    for member in assessment.get('members') or []:
        member.setdefault('focusMinutes', 0)
    # 写回：快照里 assessment 缺失或为 null 时，上面的补全必须真的落到返回对象上
    archive['assessment'] = assessment
    return archive


@bp.get('/teacher/projects')
@require_auth
def teacher_projects():
    """团队总览（仅教师/管理角色）：默认我管理的组 + 公共项目；?group=<id> 只看该组（契约 3.8）。"""
    if not is_teacher_tier(g.user):
        raise forbidden('仅教师或管理角色可访问')
    group_id = request.args.get('group')
    if group_id:
        group = query_one('SELECT * FROM groups WHERE id = ?', (group_id,))
        if not group:
            raise ApiError(404, 'GROUP_NOT_FOUND', '用户组不存在')
        if not can_manage_group(group, g.user):
            raise forbidden('仅可查看自己管理范围内的组')
        rows = query_all('SELECT * FROM projects WHERE groupId = ? ORDER BY updatedAt DESC', (group_id,))
    elif g.user['role'] in ('schooladmin', 'admin', 'superadmin'):
        scope = school_scope(g.user)
        if scope is None:
            rows = query_all('SELECT * FROM projects ORDER BY updatedAt DESC')
        else:
            # 本校分组的项目 + 未归属分组的公共项目
            rows = query_all(
                'SELECT * FROM projects WHERE groupId IS NULL OR groupId IN '
                '(SELECT id FROM groups WHERE schoolId IS NULL OR schoolId = ?) '
                'ORDER BY updatedAt DESC',
                (scope,),
            )
    else:
        rows = query_all(
            'SELECT * FROM projects WHERE groupId IS NULL OR groupId IN '
            "(SELECT groupId FROM group_members WHERE userId = ? AND role = 'teacher') "
            'ORDER BY updatedAt DESC',
            (g.user['id'],),
        )
    return jsonify(paged(project_views(rows, g.user)))


@bp.get('/projects/<project_id>/annotations')
@require_auth
def list_annotations(project_id):
    """批注列表：学生（成员）只读、教师可读（契约 3.8）。"""
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    return jsonify(paged(annotations_with_tasks(project['id'])))


@bp.post('/annotations/<annotation_id>/tasks')
@require_auth
def create_task_from_annotation(annotation_id):
    """把教师批注转成任务（契约 3.8）：形成「批注 → 任务 → 验收 → 档案」的可追溯链路。"""
    if not is_teacher_tier(g.user):
        raise forbidden('仅教师或管理角色可将批注转为任务')
    annotation = query_one('SELECT * FROM annotations WHERE id = ?', (annotation_id,))
    if not annotation:
        raise not_found('批注不存在')
    project = get_project_or_404(annotation['projectId'])
    if not can_review(project, g.user):
        raise forbidden('仅负责教师可将批注转为任务')
    ensure_active(project['id'])
    body = get_json_body()
    title = optional_text(body.get('title'), '任务标题', 200, strip=True) or annotation['content'][:200]
    if not title:
        raise bad_request('任务标题不能为空')
    description = optional_text(body.get('description'), '任务描述', 5000) \
        or f'来自教师批注：{annotation["content"]}'
    assignee = body.get('assigneeId')
    if assignee is not None and (not isinstance(assignee, str) or not query_one(
        'SELECT 1 FROM members WHERE projectId = ? AND userId = ?', (project['id'], assignee)
    )):
        raise bad_request('负责人必须是本项目成员，或留空由学生认领')
    now = now_iso()
    task = {
        'id': gen_id('t'), 'projectId': project['id'], 'title': title, 'description': description,
        'assigneeId': assignee, 'status': 'todo', 'dueDate': due_date(body.get('dueDate')),
        'createdAt': now, 'updatedAt': now, 'statusChangedAt': now,
        'sourceAnnotationId': annotation['id'],
        'criteria': criteria_value(body.get('criteria')),
        'verifiedBy': None, 'verifiedAt': None,
    }
    execute('INSERT INTO tasks (id, projectId, title, description, assigneeId, status, dueDate, '
            'createdAt, updatedAt, statusChangedAt, sourceAnnotationId, criteria, verifiedBy, verifiedAt) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', tuple(task.values()))
    add_task_log(project['id'], task['id'], g.user, 'create', '由教师批注创建任务')
    touch_project(project['id'])
    commit()
    return jsonify(task_view(task)), 201


@bp.post('/projects/<project_id>/annotations')
@require_auth
def create_annotation(project_id):
    """添加批注（仅教师/管理角色，契约 3.8）。"""
    if not is_teacher_tier(g.user):
        raise forbidden('仅教师或管理角色可添加批注')
    project = get_project_or_404(project_id)
    if not can_review(project, g.user):
        raise forbidden('仅负责教师可添加批注')
    body = get_json_body()
    content = optional_text(body.get('content'), '批注内容', 2000, strip=True)
    if not content:
        raise bad_request('批注内容不能为空')
    annotation = {'id': gen_id('a'), 'projectId': project['id'], 'userId': g.user['id'],
                  'content': content, 'createdAt': now_iso()}
    execute('INSERT INTO annotations (id, projectId, userId, content, createdAt) VALUES (?, ?, ?, ?, ?)',
            tuple(annotation.values()))
    commit()
    return jsonify({**annotation, 'userName': g.user['name']}), 201


@bp.get('/projects/<project_id>/archive')
@require_auth
def get_archive(project_id):
    """科创档案（派生资源，仅已结题项目，契约 2.13 / 3.9）。"""
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    if project['status'] != 'finished':
        raise ApiError(409, 'PROJECT_NOT_FINISHED', '项目结题后即可生成科创档案')
    snapshot = query_one('SELECT content FROM project_archives WHERE projectId = ?', (project_id,))
    if snapshot:
        archive = normalize_archive(json_loads(snapshot['content']))
        archive['project']['permissions'] = project_view(project)['permissions']
        return jsonify(overlay_live_evaluations(archive, project_id))
    return jsonify(normalize_archive(build_archive(project)))


def build_archive(project):

    members = []
    for u in project_members(project['id']):
        mine = query_all('SELECT * FROM tasks WHERE projectId = ? AND assigneeId = ?',
                         (project['id'], u['id']))
        checkins = query_all('SELECT * FROM checkins WHERE projectId = ? AND userId = ?',
                             (project['id'], u['id']))
        focus_minutes = query_one(
            "SELECT COALESCE(SUM(f.durationMin), 0) AS m FROM focus_sessions f "
            "JOIN tasks t ON t.id = f.taskId WHERE t.projectId = ? AND f.userId = ? AND f.type = 'focus'",
            (project['id'], u['id']))['m']
        members.append({
            'user': u,
            'taskCount': len(mine),
            'doneCount': sum(1 for t in mine if t['status'] == 'done'),
            'checkinCount': len(checkins),
            'focusMinutes': focus_minutes,
        })

    def parse(iso_str: str) -> datetime:
        return datetime.fromisoformat(iso_str.replace('Z', '+00:00'))

    duration_days = round((parse(project['finishedAt']) - parse(project['createdAt'])).total_seconds() / 86400)

    tasks = query_all('SELECT * FROM tasks WHERE projectId = ?', (project['id'],))
    from ..assessment import assess_project
    return {
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
        'annotations': annotations_with_tasks(project['id']),
        # 同伴互评与教师量化评分是判断依据的一部分，随档案一并留档
        'peerReviews': query_all(
            'SELECT r.*, u.name AS reviewerName, t.title AS taskTitle FROM task_reviews r '
            'LEFT JOIN users u ON u.id = r.reviewerId LEFT JOIN tasks t ON t.id = r.taskId '
            'WHERE r.projectId = ? ORDER BY r.createdAt', (project['id'],)),
        'evaluations': [
            evaluation_view(row) for row in query_all(
                'SELECT e.*, u.name AS userName, v.name AS evaluatorName FROM evaluations e '
                'LEFT JOIN users u ON u.id = e.userId LEFT JOIN users v ON v.id = e.evaluatorId '
                'WHERE e.projectId = ? ORDER BY u.name', (project['id'],))
        ],
        'assessment': assess_project(project),
    }


@bp.get('/projects/<project_id>/assessment')
@require_auth
def project_assessment(project_id):
    from ..assessment import assess_project, record_snapshot
    project = get_project_or_404(project_id)
    ensure_read_access(project_id, g.user)
    snapshot = query_one('SELECT content FROM project_archives WHERE projectId = ?', (project_id,))
    if snapshot:
        frozen = normalize_archive(json_loads(snapshot['content']))
        # 自动指标用结题快照，教师评分取当前值（可补录）
        frozen['assessment']['evaluation'] = overlay_live_evaluations({}, project_id)['evaluation']
        return jsonify(frozen['assessment'])
    result = assess_project(project)
    # 进行中的项目每天留一份快照，供次日对比趋势；已结题项目的结论由档案快照固定
    if project['status'] == 'active':
        try:
            record_snapshot(project, result)
            commit()
        except Exception:  # noqa: BLE001 - 快照是附带产物，失败不应影响评价本身
            current_app.logger.warning('assessment snapshot failed', exc_info=True)
    return jsonify(result)


@bp.post('/projects/<project_id>/risk-responses')
@require_auth
def create_risk_response(project_id):
    """成员对预警补充说明（契约 3.9）：把单向报告变成可核实的过程记录。"""
    from ..assessment import RISK_CODES
    project = get_project_or_404(project_id)
    member_of(project['id'], g.user)
    body = get_json_body()
    code = body.get('riskCode')
    if code not in RISK_CODES:
        raise bad_request('预警编号无效')
    content = optional_text(body.get('content'), '补充说明', 500, strip=True)
    if not content:
        raise bad_request('补充说明不能为空')
    # 可选证据：引用本项目的一条打卡或一项任务，让说明不止是一句话
    evidence_type, evidence_id = body.get('evidenceType'), body.get('evidenceId')
    if evidence_type is not None or evidence_id is not None:
        if evidence_type not in ('checkin', 'task') or not isinstance(evidence_id, str):
            raise bad_request('证据需为 { evidenceType: checkin|task, evidenceId }')
        table = 'checkins' if evidence_type == 'checkin' else 'tasks'
        if not query_one(f'SELECT 1 FROM {table} WHERE id = ? AND projectId = ?',
                         (evidence_id, project['id'])):
            raise bad_request('引用的证据不在本项目内')
    response = {'id': gen_id('rr'), 'projectId': project['id'], 'riskCode': code,
                'userId': g.user['id'], 'content': content, 'createdAt': now_iso(),
                'evidenceType': evidence_type, 'evidenceId': evidence_id}
    execute('INSERT INTO risk_responses (id, projectId, riskCode, userId, content, createdAt, '
            'evidenceType, evidenceId) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', tuple(response.values()))
    commit()
    return jsonify({**response, 'userName': g.user['name'], 'evidenceLabel': None}), 201
