"""同伴互评与教师量化评分（契约 3.13）。

两者解决的是同一类缺口：过程评价此前只有"可自动计算"的指标，队友之间的判断与
教师的主观评价都没有落点。这里把它们变成有作者、有时间、可追溯的记录：

- 同伴互评由项目成员发起，记录「认可」或「疑问」，不替代教师验收的权威结论。
- 教师量化评分是结题时的多维打分，补齐「教师量化评分没有可靠数据」这一缺口。
"""
from flask import Blueprint, g, jsonify

from ..auth import require_auth
from ..db import commit, execute, gen_id, json_dumps, json_loads, now_iso, query_all, query_one
from ..errors import ApiError, bad_request, forbidden, get_json_body
from ..services import (
    add_audit,
    add_feedback,
    can_review,
    ensure_active,
    ensure_read_access,
    get_project_or_404,
    get_task_or_404,
    touch_project,
)
from ..validation import optional_text

bp = Blueprint('review', __name__)

# 教师量化评分的维度与分值范围：维度固定，便于横向比较，也避免各自造词
EVAL_DIMENSIONS = (
    ('problem', '问题理解'),
    ('solution', '方案与创新'),
    ('collaboration', '协作与过程'),
    ('presentation', '表达与呈现'),
)
EVAL_MIN, EVAL_MAX = 1, 5
PEER_VERDICTS = ('acknowledge', 'question')


def task_reviews(task_id: str) -> list[dict]:
    return query_all(
        'SELECT r.*, u.name AS reviewerName FROM task_reviews r '
        'LEFT JOIN users u ON u.id = r.reviewerId WHERE r.taskId = ? ORDER BY r.createdAt',
        (task_id,),
    )


# ---------------------------------------------------------------- 同伴互评

@bp.get('/tasks/<task_id>/reviews')
@require_auth
def list_task_reviews(task_id):
    """某项任务的同伴互评（项目读取权限）。"""
    task = get_task_or_404(task_id)
    ensure_read_access(task['projectId'], g.user)
    return jsonify({'items': task_reviews(task_id)})


@bp.post('/tasks/<task_id>/reviews')
@require_auth
def create_task_review(task_id):
    """成员对队友提交的成果给出认可或疑问。

    只在任务进入待验收或已完成时可评：此时成果已经提交，评价才有对象。
    不能评自己的任务；同一人对同一任务只保留一条（可重复提交以更新）。
    """
    task = get_task_or_404(task_id)
    project = get_project_or_404(task['projectId'])
    ensure_active(project['id'])
    if not query_one('SELECT 1 FROM members WHERE projectId = ? AND userId = ?',
                     (project['id'], g.user['id'])):
        raise forbidden('仅项目成员可互评')
    if task['assigneeId'] == g.user['id']:
        raise forbidden('不能评价自己的任务')
    if task['status'] not in ('review', 'done'):
        raise ApiError(409, 'TASK_NOT_SUBMITTED', '任务提交验收后才能互评')
    body = get_json_body()
    verdict = body.get('verdict')
    if verdict not in PEER_VERDICTS:
        raise bad_request('verdict 仅支持 acknowledge 或 question')
    comment = optional_text(body.get('comment'), '评语', 500, strip=True)
    if verdict == 'question' and not comment:
        raise bad_request('提出疑问时请说明具体问题')
    existing = query_one('SELECT * FROM task_reviews WHERE taskId = ? AND reviewerId = ?',
                         (task_id, g.user['id']))
    if existing:
        # 重复提交视为更新，避免同一人刷出一串记录
        execute('UPDATE task_reviews SET verdict = ?, comment = ?, createdAt = ? WHERE id = ?',
                (verdict, comment, now_iso(), existing['id']))
    else:
        execute('INSERT INTO task_reviews (id, taskId, projectId, reviewerId, verdict, comment, createdAt) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (gen_id('tr'), task_id, project['id'], g.user['id'], verdict, comment, now_iso()))
        add_feedback(project['id'], g.user, 'guide',
                     f"同伴对任务「{task['title']}」{'提出了疑问' if verdict == 'question' else '表示认可'}。")
    touch_project(project['id'])
    commit()
    rows = [r for r in task_reviews(task_id) if r['reviewerId'] == g.user['id']]
    return jsonify(rows[0] if rows else {}), 201


# ---------------------------------------------------------------- 教师量化评分

def validate_dimensions(value) -> tuple[str, int]:
    """校验维度打分，返回 (JSON 字符串, 总分)。

    四个维度必须齐全：缺项会让"总分"失去可比性，也让教师少看一个方面。
    """
    if not isinstance(value, dict):
        raise bad_request('dimensions 需为对象')
    unknown = set(value) - {key for key, _ in EVAL_DIMENSIONS}
    if unknown:
        raise bad_request(f'未知评分维度：{", ".join(sorted(unknown))}')
    scores = {}
    for key, label in EVAL_DIMENSIONS:
        score = value.get(key)
        if isinstance(score, bool) or not isinstance(score, int) or not EVAL_MIN <= score <= EVAL_MAX:
            raise bad_request(f'「{label}」需为 {EVAL_MIN}~{EVAL_MAX} 的整数')
        scores[key] = score
    return json_dumps(scores), sum(scores.values())


def evaluation_view(row: dict) -> dict:
    dimensions = json_loads(row['dimensions']) if row['dimensions'] else {}
    return {**row, 'dimensions': dimensions,
            'total': sum(dimensions.values()) if isinstance(dimensions, dict) else 0,
            'maxTotal': len(EVAL_DIMENSIONS) * EVAL_MAX}


@bp.get('/projects/<project_id>/evaluations')
@require_auth
def list_evaluations(project_id):
    """教师量化评分：负责教师/管理角色可见全部，学生只看自己那份。"""
    project = get_project_or_404(project_id)
    ensure_read_access(project_id, g.user)
    if can_review(project, g.user):
        rows = query_all(
            'SELECT e.*, u.name AS userName, v.name AS evaluatorName FROM evaluations e '
            'LEFT JOIN users u ON u.id = e.userId LEFT JOIN users v ON v.id = e.evaluatorId '
            'WHERE e.projectId = ? ORDER BY u.name', (project_id,))
    else:
        # 学生只看自己的评分，避免把同伴之间的分数摊开比较
        rows = query_all(
            'SELECT e.*, u.name AS userName, v.name AS evaluatorName FROM evaluations e '
            'LEFT JOIN users u ON u.id = e.userId LEFT JOIN users v ON v.id = e.evaluatorId '
            'WHERE e.projectId = ? AND e.userId = ?', (project_id, g.user['id']))
    return jsonify({'items': [evaluation_view(r) for r in rows], 'total': len(rows),
                    'dimensions': [{'key': k, 'label': label} for k, label in EVAL_DIMENSIONS],
                    'min': EVAL_MIN, 'max': EVAL_MAX})


@bp.put('/projects/<project_id>/evaluations/<user_id>')
@require_auth
def upsert_evaluation(project_id, user_id):
    """写入或更新某成员的量化评分（仅负责教师/管理角色）。"""
    project = get_project_or_404(project_id)
    if not can_review(project, g.user):
        raise forbidden('仅负责教师可评分')
    if not query_one('SELECT 1 FROM members WHERE projectId = ? AND userId = ?', (project_id, user_id)):
        raise ApiError(404, 'NOT_FOUND', '该成员不在项目中')
    body = get_json_body()
    dimensions, total = validate_dimensions(body.get('dimensions'))
    comment = optional_text(body.get('comment'), '评语', 1000, strip=True)
    now = now_iso()
    existing = query_one('SELECT * FROM evaluations WHERE projectId = ? AND userId = ? AND evaluatorId = ?',
                         (project_id, user_id, g.user['id']))
    if existing:
        execute('UPDATE evaluations SET dimensions = ?, comment = ?, updatedAt = ? WHERE id = ?',
                (dimensions, comment, now, existing['id']))
    else:
        execute('INSERT INTO evaluations (id, projectId, userId, evaluatorId, dimensions, comment, '
                'createdAt, updatedAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (gen_id('ev'), project_id, user_id, g.user['id'], dimensions, comment, now, now))
    add_audit(g.user, 'evaluation.upsert', 'project', project_id,
              f'为成员 {user_id} 评分（总分 {total}）')
    touch_project(project_id)
    commit()
    row = query_one(
        'SELECT e.*, u.name AS userName, v.name AS evaluatorName FROM evaluations e '
        'LEFT JOIN users u ON u.id = e.userId LEFT JOIN users v ON v.id = e.evaluatorId '
        'WHERE e.projectId = ? AND e.userId = ? AND e.evaluatorId = ?',
        (project_id, user_id, g.user['id']))
    return jsonify(evaluation_view(row)), 200


@bp.delete('/projects/<project_id>/evaluations/<user_id>')
@require_auth
def delete_evaluation(project_id, user_id):
    """撤销自己给出的评分（仅本人）。"""
    project = get_project_or_404(project_id)
    if not can_review(project, g.user):
        raise forbidden('仅负责教师可评分')
    row = query_one('SELECT id FROM evaluations WHERE projectId = ? AND userId = ? AND evaluatorId = ?',
                    (project_id, user_id, g.user['id']))
    if not row:
        raise ApiError(404, 'NOT_FOUND', '评分不存在')
    execute('DELETE FROM evaluations WHERE id = ?', (row['id'],))
    add_audit(g.user, 'evaluation.delete', 'project', project_id, f'撤销对成员 {user_id} 的评分')
    touch_project(project_id)
    commit()
    return '', 204


def evaluation_summary(project_id: str) -> dict:
    """项目评分汇总：按成员汇总后再求平均，供档案与评价使用。

    先按成员求各自均分，再对成员求平均：若直接对所有评分行求平均，
    被两位教师评过分的成员会被重复计入，人数与均分都会失真。
    """
    rows = query_all('SELECT userId, dimensions FROM evaluations WHERE projectId = ?', (project_id,))
    if not rows:
        return {'count': 0, 'members': 0, 'avgTotal': None, 'dimensions': {}}
    per_member: dict[str, list[dict]] = {}
    for row in rows:
        data = json_loads(row['dimensions'])
        if isinstance(data, dict) and data:
            per_member.setdefault(row['userId'], []).append(data)
    if not per_member:
        return {'count': len(rows), 'members': 0, 'avgTotal': None, 'dimensions': {}}

    member_totals = []
    per_dimension: dict[str, list[float]] = {key: [] for key, _ in EVAL_DIMENSIONS}
    for entries in per_member.values():
        member_totals.append(sum(sum(e.values()) for e in entries) / len(entries))
        for key in per_dimension:
            values = [e[key] for e in entries if isinstance(e.get(key), int)]
            if values:
                per_dimension[key].append(sum(values) / len(values))
    return {
        'count': len(rows),
        'members': len(per_member),
        'avgTotal': round(sum(member_totals) / len(member_totals), 1),
        'dimensions': {key: round(sum(vals) / len(vals), 1) for key, vals in per_dimension.items() if vals},
    }


# ---------------------------------------------------------------- 互评汇总

def peer_review_summary(project_id: str) -> dict:
    """互评概览：认可与疑问的数量，以及被提出疑问的任务。"""
    rows = query_all('SELECT taskId, verdict FROM task_reviews WHERE projectId = ?', (project_id,))
    return {
        'total': len(rows),
        'acknowledge': sum(1 for r in rows if r['verdict'] == 'acknowledge'),
        'question': sum(1 for r in rows if r['verdict'] == 'question'),
        'questionedTaskIds': sorted({r['taskId'] for r in rows if r['verdict'] == 'question'}),
    }
