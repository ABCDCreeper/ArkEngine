"""打卡与动态反馈路由（契约 3.5）：打卡、系统反馈列表、修订号与变更推送。"""
import json
import sqlite3
import time

from flask import Blueprint, Response, current_app, g, jsonify, stream_with_context

from ..auth import require_auth
from ..db import commit, execute, gen_id, get_revision, now_iso, query_all, query_one
from ..errors import bad_request, get_json_body
from ..validation import optional_text
from ..services import (    add_feedback,
    ensure_read_access,
    get_project_or_404,
    member_of,
    paged,
    pick_feedback,
    touch_project,
)

bp = Blueprint('activity', __name__)


@bp.get('/projects/<project_id>/revision')
@require_auth
def project_revision(project_id):
    """项目数据修订号（读取权限即可）。

    轮询的廉价形态：客户端只需比较这个数字，不变就不必重新拉取整个看板。
    """
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    return jsonify({'revision': get_revision(project_id)})


# 单条 SSE 连接的最长存活时间（秒）：到点主动结束，由 EventSource 自动重连，
# 避免长期占用开发服务器的线程。
STREAM_MAX_SECONDS = 240
STREAM_INTERVAL_SECONDS = 1.5


@bp.get('/projects/<project_id>/stream')
@require_auth
def project_stream(project_id):
    """项目变更推送（SSE）：修订号变化时推一个事件，客户端据此刷新。

    只推"数据变了"这一个信号，不推数据本身——内容仍走常规接口，
    权限校验与响应格式保持一致。
    """
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    interval = current_app.config.get('STREAM_INTERVAL_SECONDS', STREAM_INTERVAL_SECONDS)
    max_seconds = current_app.config.get('STREAM_MAX_SECONDS', STREAM_MAX_SECONDS)

    def events():
        started = time.monotonic()
        last = get_revision(project_id)
        yield f'event: revision\ndata: {json.dumps({"revision": last})}\n\n'
        while time.monotonic() - started < max_seconds:
            time.sleep(interval)
            current = get_revision(project_id)
            if current != last:
                last = current
                yield f'event: revision\ndata: {json.dumps({"revision": current})}\n\n'
            else:
                yield ': keep-alive\n\n'
        yield 'event: reconnect\ndata: {}\n\n'

    response = Response(stream_with_context(events()), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


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
    """打卡成功同时生成一条 guide 类型系统反馈（契约 3.5）。

    可选 `clientId`：离线补交时由客户端生成，同一 clientId 只会入库一次，
    因此断网重试不会产生重复打卡。
    """
    project = get_project_or_404(project_id)
    member_of(project['id'], g.user)
    body = get_json_body()
    content = optional_text(body.get('content'), '打卡内容', 2000, strip=True)
    if not content:
        raise bad_request('打卡内容不能为空')
    client_id = body.get('clientId')
    if client_id is not None:
        client_id = optional_text(client_id, '打卡标识', 64, strip=True) or None
    if client_id:
        existing = query_one('SELECT * FROM checkins WHERE userId = ? AND clientId = ?',
                             (g.user['id'], client_id))
        if existing:
            # 断网重试：返回已入库的那条，不重复生成打卡与反馈
            return jsonify(existing), 200
    checkin = {'id': gen_id('c'), 'projectId': project['id'], 'userId': g.user['id'],
               'content': content, 'createdAt': now_iso(), 'clientId': client_id}
    try:
        execute('INSERT INTO checkins (id, projectId, userId, content, createdAt, clientId) '
                'VALUES (?, ?, ?, ?, ?, ?)', tuple(checkin.values()))
    except sqlite3.IntegrityError:
        # 并发重试撞上唯一索引：同样按已入库处理
        existing = query_one('SELECT * FROM checkins WHERE userId = ? AND clientId = ?',
                             (g.user['id'], client_id))
        if existing:
            return jsonify(existing), 200
        raise
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
