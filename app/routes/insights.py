"""通知中心与教学总览（契约 3.14）。

通知不落新表：由既有数据（批注、被退回的任务、待验收、到期任务、互评、邀请、到期错题）
按时间派生，只需记住"看到哪一刻"。这样既不会漏报，也不会出现通知与事实不一致。

教学总览把单项目的评价规则聚合到教师/学校层面：哪些项目有需处理的风险、
学生参与情况如何，供教师在课堂层面做干预。
"""
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify

from ..auth import require_auth
from ..db import commit, execute, iso, now_iso, query_all, query_one
from ..errors import bad_request, forbidden, get_json_body
from ..services import is_teacher_tier, reviewable_project_ids, school_scope
from ..assessment import assess_project

bp = Blueprint('insights', __name__)

# 单次最多返回的通知条数
NOTIFICATION_LIMIT = 50


def cursor_for(user_id: str) -> str:
    row = query_one('SELECT seenAt FROM notification_cursors WHERE userId = ?', (user_id,))
    return row['seenAt'] if row else '1970-01-01T00:00:00.000000Z'


@bp.get('/notifications')
@require_auth
def list_notifications():
    """我的通知：由既有数据派生并标注是否已读。"""
    seen_at = cursor_for(g.user['id'])
    items: list[dict] = []

    def add(kind: str, title: str, detail: str, created_at: str, link: str) -> None:
        items.append({'type': kind, 'title': title, 'detail': detail,
                      'createdAt': created_at, 'link': link, 'unread': created_at > seen_at})

    # 我参与的项目
    project_ids = [r['projectId'] for r in query_all(
        'SELECT projectId FROM members WHERE userId = ?', (g.user['id'],))]
    if project_ids:
        marks = ','.join('?' * len(project_ids))
        # 新批注
        for row in query_all(
            f'SELECT a.projectId, a.content, a.createdAt, p.name FROM annotations a '
            f'JOIN projects p ON p.id = a.projectId WHERE a.projectId IN ({marks}) '
            f'ORDER BY a.createdAt DESC LIMIT 10', project_ids):
            add('annotation', f'「{row["name"]}」有新批注', row['content'][:60], row['createdAt'],
                f'/project/{row["projectId"]}?tab=annotations')
        # 被退回的任务：必须由"别人"把状态改回进行中。
        # 学生自己认领（待认领→进行中）或撤回提交（待验收→进行中）写的是同一条动态，
        # 不加这层区分就会把自己的操作误报成"被教师退回"。
        # 同一任务可能被退回多次，按任务取最近一次，避免刷出一串重复通知。
        for row in query_all(
            f'SELECT l.taskId, l.projectId, MAX(l.createdAt) AS createdAt, t.title, u.name AS actorName '
            f'FROM task_logs l JOIN tasks t ON t.id = l.taskId '
            f'LEFT JOIN users u ON u.id = l.userId '
            f'WHERE l.projectId IN ({marks}) AND t.assigneeId = ? AND t.status != ? '
            f'AND l.action = ? AND l.detail = ? AND l.userId != t.assigneeId '
            f'GROUP BY l.taskId ORDER BY createdAt DESC LIMIT 10',
            [*project_ids, g.user['id'], 'done', 'status', '状态更新为 进行中']):
            add('returned', f'任务「{row["title"]}」被退回',
                f'{row["actorName"] or "负责教师"}把任务退回，请按意见修改后重新提交',
                row['createdAt'], f'/project/{row["projectId"]}?tab=tasks')
        # 已逾期的任务：时间戳用截止时间本身。
        # 这是一个已经发生过的时刻，因此未读与排序都合理；若改成"任务最后变更时间"，
        # 一条长期没人动的逾期任务会永远显示为已读。
        for row in query_all(
            f'SELECT t.id, t.projectId, t.title, t.dueDate FROM tasks t '
            f'WHERE t.projectId IN ({marks}) AND t.assigneeId = ? AND t.status != ? '
            f'AND t.dueDate IS NOT NULL AND t.dueDate < ? ORDER BY t.dueDate LIMIT 10',
            [*project_ids, g.user['id'], 'done', now_iso()]):
            add('due', f'任务「{row["title"]}」已逾期', f'截止 {row["dueDate"][:10]}', row['dueDate'],
                f'/project/{row["projectId"]}?tab=tasks')
        # 我的任务收到同伴互评
        for row in query_all(
            f'SELECT r.taskId, r.projectId, r.verdict, r.comment, r.createdAt, t.title, u.name '
            f'FROM task_reviews r JOIN tasks t ON t.id = r.taskId '
            f'JOIN users u ON u.id = r.reviewerId '
            f'WHERE r.projectId IN ({marks}) AND t.assigneeId = ? '
            f'ORDER BY r.createdAt DESC LIMIT 10', [*project_ids, g.user['id']]):
            add('peer_review', f'{row["name"]}对「{row["title"]}」'
                f'{"提出疑问" if row["verdict"] == "question" else "表示认可"}',
                row['comment'][:60] or '（无评语）', row['createdAt'],
                f'/project/{row["projectId"]}?tab=tasks')

    # 等待我验收的任务：先用一次查询取出我可验收的项目，再过滤（避免逐行判权限）
    reviewable = reviewable_project_ids(g.user)
    if reviewable is None:
        pending = query_all(
            "SELECT t.projectId, t.title, p.name, COALESCE(t.statusChangedAt, t.updatedAt) AS changedAt "
            "FROM tasks t JOIN projects p ON p.id = t.projectId "
            "WHERE t.status = 'review' AND p.status = 'active' ORDER BY t.statusChangedAt LIMIT 20")
    elif reviewable:
        marks = ','.join('?' * len(reviewable))
        pending = query_all(
            f"SELECT t.projectId, t.title, p.name, COALESCE(t.statusChangedAt, t.updatedAt) AS changedAt "
            f"FROM tasks t JOIN projects p ON p.id = t.projectId "
            f"WHERE t.status = 'review' AND p.status = 'active' AND t.projectId IN ({marks}) "
            f"ORDER BY t.statusChangedAt LIMIT 20", list(reviewable))
    else:
        pending = []
    for row in pending:
        add('verify', f'「{row["name"]}」有任务等待验收', row['title'], row['changedAt'],
            f'/project/{row["projectId"]}?tab=tasks')

    # 待我处理的入组邀请
    for row in query_all(
        'SELECT gi.id, gi.createdAt, g.name FROM group_invites gi JOIN groups g ON g.id = gi.groupId '
        "WHERE gi.userId = ? AND gi.status = 'pending' ORDER BY gi.createdAt DESC", (g.user['id'],)):
        add('invite', f'「{row["name"]}」邀请你加入', '在首页确认或拒绝', row['createdAt'], '/')

    # 到期待复习的错题
    due_wrong = query_one(
        'SELECT COUNT(*) AS c, MIN(nextReviewAt) AS since FROM quiz_wrong_answers '
        'WHERE userId = ? AND resolvedAt IS NULL AND nextReviewAt <= ?', (g.user['id'], now_iso()))
    if due_wrong and due_wrong['c']:
        # 用"最早一道题的到期时刻"：这才是复习提醒真正生效的时间点
        add('quiz', f'{due_wrong["c"]} 道错题到了复习时间', '按间隔复习能显著提高长期记忆',
            due_wrong['since'] or now_iso(), '/quiz')

    items.sort(key=lambda x: x['createdAt'], reverse=True)
    shown = items[:NOTIFICATION_LIMIT]
    # 未读数按实际返回的条目统计：徽标上的数字应当能在列表里逐条对上
    return jsonify({
        'items': shown,
        'total': len(shown),
        'unread': sum(1 for x in shown if x['unread']),
        'seenAt': seen_at,
    })


@bp.post('/notifications/seen')
@require_auth
def mark_notifications_seen():
    """把通知标记为已读（只记游标，不改动任何业务数据）。

    游标不会超过服务端当前时间：客户端时钟偏快时若照单全收，
    之后产生的通知都会落在游标之前，用户会被永久静音。
    """
    body = get_json_body()
    seen_at = body.get('seenAt')
    now = now_iso()
    if seen_at is None:
        stamp = now
    else:
        if not isinstance(seen_at, str):
            raise bad_request('seenAt 需为 ISO 时间字符串')
        try:
            # 归一化后再比较：ISO 时间可以有 Z 或 +00:00 两种写法，
            # 直接按字符串比大小会得出错误结果（'+' 与 'Z' 的字典序没有时间含义）
            moment = datetime.fromisoformat(seen_at.replace('Z', '+00:00'))
            if moment.tzinfo is None:
                raise ValueError('timezone required')
        except (ValueError, TypeError):
            raise bad_request('seenAt 需为带时区的 ISO 时间') from None
        normalized = iso(moment.astimezone(timezone.utc))
        stamp = min(normalized, now)
    execute('INSERT INTO notification_cursors (userId, seenAt) VALUES (?, ?) '
            'ON CONFLICT(userId) DO UPDATE SET seenAt = excluded.seenAt', (g.user['id'], stamp))
    commit()
    return jsonify({'seenAt': stamp})


# ---------------------------------------------------------------- 教学总览

@bp.get('/teacher/analytics')
@require_auth
def teacher_analytics():
    """教学总览（仅教师/管理角色）：把单项目的过程评价聚合到课堂层面。

    只统计我可见的项目：普通教师为自己负责的组 + 公共项目，校管理员限定在本校范围。
    """
    if not is_teacher_tier(g.user):
        raise forbidden('仅教师或管理角色可访问')
    ids = reviewable_project_ids(g.user)
    if ids is None:
        projects = query_all("SELECT * FROM projects WHERE status = 'active' ORDER BY updatedAt DESC")
    elif ids:
        marks = ','.join('?' * len(ids))
        projects = query_all(
            f"SELECT * FROM projects WHERE status = 'active' AND id IN ({marks}) "
            f"ORDER BY updatedAt DESC", list(ids))
    else:
        projects = []

    rows = []
    with_risks = 0
    for project in projects:
        report = assess_project(project)
        warnings = [r for r in report['risks'] if r['level'] == 'warning']
        if warnings:
            with_risks += 1
        rows.append({
            'project': {'id': project['id'], 'name': project['name'], 'groupId': project['groupId'],
                        'updatedAt': project['updatedAt']},
            'summary': report['summary'],
            'warningCount': len(warnings),
            'infoCount': len(report['risks']) - len(warnings),
            'topRisks': [{'code': r['code'], 'title': r['title'], 'level': r['level']} for r in report['risks'][:3]],
            'memberCount': report['summary']['memberCount'],
            'activeMembers': report['summary']['activeMembers'],
        })
    rows.sort(key=lambda r: (-r['warningCount'], -r['summary']['overdue']))
    return jsonify({
        'items': rows,
        'total': len(rows),
        'withRisks': with_risks,
        'asOf': now_iso(),
        'scopedToSchool': school_scope(g.user) if g.user['role'] != 'teacher' else None,
    })
