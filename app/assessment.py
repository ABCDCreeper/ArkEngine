"""Explainable process indicators derived from recorded project activity."""
from datetime import datetime, timedelta, timezone

from .db import execute, gen_id, json_dumps, json_loads, query_all, query_one
from .services import project_members


def peer_summary(project_id: str) -> dict:
    """同伴互评概览（同伴视角也是过程证据的一部分）。"""
    from .routes.review import peer_review_summary
    return peer_review_summary(project_id)


def evaluation_summary(project_id: str) -> dict:
    """教师量化评分汇总；没有评分时明确返回 count=0，不把缺失当零分。"""
    from .routes.review import evaluation_summary as summarize
    return summarize(project_id)

# 规则版本：计算规则变化时必须递增，历史档案的结论才能对照版本阅读
VERSION = 'process-v1'

# 允许成员补充说明的预警编号（前端据此渲染输入框，后端据此校验）
RISK_CODES = ('NO_TASKS', 'OVERDUE', 'UNASSIGNED', 'REVIEW_WAIT', 'LOW_ACTIVITY', 'CONCENTRATED')

# 每条预警最多返回的补充说明条数（保留最近 N 条），避免评价响应随记录无限增长
RESPONSE_LIMIT = 20


def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def risk_responses(project_id: str) -> list[dict]:
    """成员对预警的补充说明（附带作者姓名与可选证据）。"""
    rows = query_all(
        'SELECT r.id, r.projectId, r.riskCode, r.userId, r.content, r.createdAt, '
        'r.evidenceType, r.evidenceId, u.name AS userName '
        'FROM risk_responses r LEFT JOIN users u ON u.id = r.userId '
        'WHERE r.projectId = ? ORDER BY r.createdAt', (project_id,),
    )
    # 附上证据的可读标签：前端不必再为每条说明回查一次任务或打卡
    checkins = {c['id']: c['content'] for c in query_all(
        'SELECT id, content FROM checkins WHERE projectId = ?', (project_id,))}
    tasks = {t['id']: t['title'] for t in query_all(
        'SELECT id, title FROM tasks WHERE projectId = ?', (project_id,))}
    for row in rows:
        if row['evidenceType'] == 'checkin' and row['evidenceId'] in checkins:
            row['evidenceLabel'] = checkins[row['evidenceId']]
        elif row['evidenceType'] == 'task' and row['evidenceId'] in tasks:
            row['evidenceLabel'] = tasks[row['evidenceId']]
        else:
            # 证据被删除时保留说明本身，只把标签置空
            row['evidenceLabel'] = None
    return rows


def assess_project(project, now=None):
    now = now or datetime.now(timezone.utc)
    if project['status'] == 'finished':
        now = parse_time(project['finishedAt']) or now
    week_start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    tasks = query_all('SELECT * FROM tasks WHERE projectId = ?', (project['id'],))
    logs = query_all('SELECT * FROM task_logs WHERE projectId = ?', (project['id'],))
    checkins = query_all('SELECT * FROM checkins WHERE projectId = ?', (project['id'],))
    completed = [t for t in tasks if t['status'] == 'done']
    overdue = [t for t in tasks if t['status'] != 'done' and parse_time(t['dueDate']) and parse_time(t['dueDate']) < now]
    unassigned = [t for t in tasks if t['status'] != 'done' and not t['assigneeId']]
    review = [t for t in tasks if t['status'] == 'review']
    waiting = [t for t in review if parse_time(t.get('statusChangedAt') or t['updatedAt'])
               and parse_time(t.get('statusChangedAt') or t['updatedAt']) < now - timedelta(days=3)]
    members = []
    for user in project_members(project['id']):
        mine = [t for t in tasks if t['assigneeId'] == user['id']]
        events = [r for r in logs + checkins if r['userId'] == user['id']]
        dates = [parse_time(r['createdAt']) for r in events]
        dates = [d for d in dates if d and d <= now]
        done = sum(t['status'] == 'done' for t in mine)
        # 关联到任务的专注时长：番茄钟是学生自己启动、由系统计时产生的记录，
        # 比自述更能反映实际投入，但仍只作为参考，不等同于工作质量
        focus_minutes = query_one(
            "SELECT COALESCE(SUM(f.durationMin), 0) AS m FROM focus_sessions f "
            "JOIN tasks t ON t.id = f.taskId "
            "WHERE t.projectId = ? AND f.userId = ? AND f.type = 'focus'",
            (project['id'], user['id']))['m']
        members.append({'user': user, 'assigned': len(mine), 'completed': done,
                        'completionShare': round(done / len(completed) * 100) if completed else None,
                        'activeDays': len({d.date() for d in dates if d >= week_start}),
                        'focusMinutes': focus_minutes,
                        'lastActiveAt': max(dates).isoformat() if dates else None})
    active = sum(m['activeDays'] > 0 for m in members)
    risks = []

    def add(code, level, title, evidence, action, task_ids=None, user_ids=None):
        risks.append({'code': code, 'level': level, 'title': title, 'evidence': evidence,
                      'action': action, 'taskIds': task_ids or [], 'userIds': user_ids or []})

    if project['status'] == 'active':
        if not tasks:
            add('NO_TASKS', 'info', '尚未拆解任务', '当前项目没有任务记录。', '先确定可验证的阶段目标，再分配负责人。')
        if overdue:
            add('OVERDUE', 'warning', '任务已逾期', f'{len(overdue)} 项未完成任务已超过截止时间。',
                '核对阻碍，调整计划并记录原因。', [t['id'] for t in overdue])
        if unassigned:
            add('UNASSIGNED', 'info', '任务缺少负责人', f'{len(unassigned)} 项任务尚未认领。',
                '组内确认分工，由执行成员认领。', [t['id'] for t in unassigned])
        if waiting:
            add('REVIEW_WAIT', 'warning', '验收等待超过 3 天', f'{len(waiting)} 项任务等待教师验收。',
                '请负责教师查看成果并验收或退回。', [t['id'] for t in waiting])
        quiet = [m for m in members if not m['activeDays'] and
                 (parse_time(project['createdAt']) or now) < week_start]
        if quiet:
            add('LOW_ACTIVITY', 'info', '部分成员近 7 天无过程记录',
                '、'.join(m['user']['name'] for m in quiet) + '没有任务操作或打卡记录。',
                '与成员核实线下工作，补充实际过程证据。', user_ids=[m['user']['id'] for m in quiet])
        if len(completed) >= 3 and len(members) > 1:
            dominant = [m for m in members if (m['completionShare'] or 0) > 70]
            if dominant:
                add('CONCENTRATED', 'info', '已完成任务分配较集中',
                    '、'.join(m['user']['name'] for m in dominant) + '承担超过 70% 的已完成任务。',
                    '结合任务难度与线下分工核实负担，必要时重新分配。',
                    user_ids=[m['user']['id'] for m in dominant])
    # 补充说明按预警编号挂载：学生在界面上直接回应「与成员核实线下工作」这一建议，
    # 教师看到的是谁在何时说了什么，而不是一个无法回应的结论。
    # 说明本身不会因预警消失而删除，但它只在对应预警生效时随评价返回。
    responses = risk_responses(project['id'])
    by_code: dict[str, list[dict]] = {}
    for row in responses:
        by_code.setdefault(row['riskCode'], []).append(row)
    for risk in risks:
        # 保留最近若干条，按时间升序展示
        risk['responses'] = by_code.get(risk['code'], [])[-RESPONSE_LIMIT:]
    peer_summary_data = peer_summary(project['id'])
    result = {'version': VERSION, 'asOf': now.isoformat(), 'status': project['status'],
              'summary': {'total': len(tasks), 'done': len(completed), 'overdue': len(overdue),
                          'review': len(review), 'unassigned': len(unassigned),
                          'completionRate': round(len(completed) / len(tasks) * 100) if tasks else None,
                          'activeMembers': active, 'memberCount': len(members)},
              'members': members, 'risks': risks,
              'tasks': [{'id': t['id'], 'title': t['title']} for t in tasks],
              'peerReview': peer_summary_data,
              'evaluation': evaluation_summary(project['id'])}
    result['trend'] = compare_with_previous(project['id'], result)
    return result


# ---------------------------------------------------------------- 趋势

def snapshot_date(now: datetime) -> str:
    return now.date().isoformat()


def compare_with_previous(project_id: str, current: dict) -> dict | None:
    """与最近一份历史快照对比，给出指标变化与预警的增消。

    没有历史快照时返回 None——不编造趋势。快照每天至多一份，由读取时懒生成。
    """
    today = snapshot_date(datetime.now(timezone.utc))
    previous = query_all(
        'SELECT snapshotDate, content FROM assessment_snapshots '
        'WHERE projectId = ? AND snapshotDate < ? ORDER BY snapshotDate DESC LIMIT 1',
        (project_id, today))
    if not previous:
        return None
    try:
        before = json_loads(previous[0]['content'])
    except (TypeError, ValueError):
        return None
    if not isinstance(before, dict):
        return None

    def metrics(data: dict) -> dict:
        summary = data.get('summary') or {}
        return {'done': summary.get('done', 0), 'total': summary.get('total', 0),
                'overdue': summary.get('overdue', 0), 'review': summary.get('review', 0),
                'unassigned': summary.get('unassigned', 0),
                'activeMembers': summary.get('activeMembers', 0)}

    now_m, was_m = metrics(current), metrics(before)
    was_codes = {r['code'] for r in (before.get('risks') or []) if isinstance(r, dict)}
    now_codes = {r['code'] for r in current['risks']}
    return {
        'baseDate': previous[0]['snapshotDate'],
        'metrics': {key: {'now': now_m[key], 'before': was_m[key], 'delta': now_m[key] - was_m[key]}
                    for key in now_m},
        'risksAdded': sorted(now_codes - was_codes),
        'risksResolved': sorted(was_codes - now_codes),
    }


def record_snapshot(project: dict, assessment: dict) -> None:
    """把今天的评价写入快照（每天至多一条，已存在则不覆盖）。

    存的是当天第一次读取时的状态，因此趋势反映的是"相对昨天"的变化，
    而不是同一天内反复刷新的抖动。
    """
    today = snapshot_date(datetime.now(timezone.utc))
    execute(
        'INSERT OR IGNORE INTO assessment_snapshots (id, projectId, snapshotDate, content, createdAt) '
        'VALUES (?, ?, ?, ?, ?)',
        (gen_id('as'), project['id'], today,
         json_dumps({k: v for k, v in assessment.items() if k != 'trend'}),
         datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')),
    )
