"""沉浸式专注模式路由（契约 3.7）：番茄钟上报、我的记录、专注统计。"""
import math
from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request

from ..auth import require_auth
from ..db import commit, execute, gen_id, now_iso, query_all, query_one
from ..errors import bad_request, get_json_body

bp = Blueprint('focus', __name__)

# 单次专注/休息时长上限（分钟）：番茄钟不会超过 4 小时，超出即为异常输入
MAX_DURATION_MIN = 240


@bp.post('/focus-sessions')
@require_auth
def create_focus_session():
    """上报一次专注/休息；可选关联任务，把时长变成任务上的过程证据。"""
    body = get_json_body()
    try:
        duration = float(body.get('durationMin'))
    except (TypeError, ValueError):
        raise bad_request('时长必须为数字') from None
    if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION_MIN:
        raise bad_request(f'时长必须为 0 到 {MAX_DURATION_MIN} 分钟之间')
    if duration == int(duration):
        duration = int(duration)
    ftype = 'break' if body.get('type') == 'break' else 'focus'
    # 只能关联自己参与的项目里的任务，避免把时长记到别人的任务上
    task_id = body.get('taskId')
    if task_id is not None:
        if not isinstance(task_id, str) or not task_id:
            raise bad_request('taskId 无效')
        linked = query_one(
            'SELECT t.id FROM tasks t JOIN members m ON m.projectId = t.projectId '
            'WHERE t.id = ? AND m.userId = ?', (task_id, g.user['id']))
        if not linked:
            raise bad_request('只能关联自己参与的项目中的任务')
    session = {'id': gen_id('fs'), 'userId': g.user['id'], 'durationMin': duration,
               'type': ftype, 'createdAt': now_iso(), 'taskId': task_id}
    execute('INSERT INTO focus_sessions (id, userId, durationMin, type, createdAt, taskId) '
            'VALUES (?, ?, ?, ?, ?, ?)', tuple(session.values()))
    commit()
    return jsonify(session), 201


@bp.get('/focus-sessions')
@require_auth
def list_focus_sessions():
    items = query_all('SELECT * FROM focus_sessions WHERE userId = ? ORDER BY createdAt DESC', (g.user['id'],))
    return jsonify({'items': items, 'total': len(items), 'page': 1, 'pageSize': len(items)})


@bp.get('/focus/stats')
@require_auth
def focus_stats():
    """近 N 天专注统计（默认 7，最大 30）：无记录日期补 0，按日期升序（契约 3.7）。

    自然日按 `tzOffset`（分钟，东为正，如北京 +480）划分，默认 0 即 UTC。
    前端传 `-new Date().getTimezoneOffset()`，否则「今日专注」会在本地早上 8 点才翻页。
    """
    try:
        days = min(max(int(request.args.get('days', 7)), 1), 30)
    except ValueError:
        days = 7
    try:
        offset = min(max(int(request.args.get('tzOffset', 0)), -14 * 60), 14 * 60)
    except ValueError:
        offset = 0
    tz = timezone(timedelta(minutes=offset))
    mine = query_all(
        "SELECT createdAt, durationMin FROM focus_sessions WHERE userId = ? AND type = 'focus'",
        (g.user['id'],),
    )

    def day_key(value: str) -> str:
        try:
            moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (AttributeError, ValueError):
            return ''
        return moment.astimezone(tz).date().isoformat()

    now_local = datetime.now(tz)
    today_key = now_local.date().isoformat()
    today = [s for s in mine if day_key(s['createdAt']) == today_key]
    week = []
    for i in range(days - 1, -1, -1):
        key = (now_local - timedelta(days=i)).date().isoformat()
        day_sessions = [s for s in mine if day_key(s['createdAt']) == key]
        week.append({'date': key, 'count': len(day_sessions),
                     'minutes': sum(s['durationMin'] for s in day_sessions)})
    return jsonify({
        'today': {'count': len(today), 'minutes': sum(s['durationMin'] for s in today)},
        'week': week,
    })
