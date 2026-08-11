"""沉浸式专注模式路由（契约 3.7）：番茄钟上报、我的记录、专注统计。"""
import math
from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request

from ..auth import require_auth
from ..db import commit, execute, gen_id, now_iso, query_all
from ..errors import bad_request, get_json_body

bp = Blueprint('focus', __name__)


@bp.post('/focus-sessions')
@require_auth
def create_focus_session():
    body = get_json_body()
    try:
        duration = float(body.get('durationMin'))
    except (TypeError, ValueError):
        raise bad_request('时长必须为正整数')
    if not math.isfinite(duration) or duration <= 0:
        raise bad_request('时长必须为正整数')
    if duration == int(duration):
        duration = int(duration)
    ftype = 'break' if body.get('type') == 'break' else 'focus'
    session = {'id': gen_id('fs'), 'userId': g.user['id'], 'durationMin': duration,
               'type': ftype, 'createdAt': now_iso()}
    execute('INSERT INTO focus_sessions (id, userId, durationMin, type, createdAt) VALUES (?, ?, ?, ?, ?)',
            tuple(session.values()))
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
    """近 N 天专注统计（默认 7，最大 30）：无记录日期补 0，按日期升序（契约 3.7）。"""
    try:
        days = min(max(int(request.args.get('days', 7)), 1), 30)
    except ValueError:
        days = 7
    mine = query_all(
        "SELECT createdAt, durationMin FROM focus_sessions WHERE userId = ? AND type = 'focus'",
        (g.user['id'],),
    )
    today_key = now_iso()[:10]
    today = [s for s in mine if s['createdAt'][:10] == today_key]
    week = []
    for i in range(days - 1, -1, -1):
        key = (datetime.now(timezone.utc) - timedelta(days=i)).date().isoformat()
        day_sessions = [s for s in mine if s['createdAt'][:10] == key]
        week.append({'date': key, 'count': len(day_sessions),
                     'minutes': sum(s['durationMin'] for s in day_sessions)})
    return jsonify({
        'today': {'count': len(today), 'minutes': sum(s['durationMin'] for s in today)},
        'week': week,
    })
