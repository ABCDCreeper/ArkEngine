"""知识闯关路由（契约 3.10）：随机抽题、成绩记录与统计。"""
import random

from flask import Blueprint, g, jsonify, request

from ..auth import require_auth
from ..db import commit, execute, gen_id, json_loads, now_iso, query_all, query_one
from ..errors import bad_request, forbidden

bp = Blueprint('quiz', __name__)

MAX_QUESTIONS = 20


def question_view(row: dict) -> dict:
    return {**row, 'options': json_loads(row['options'])}


def best_view(rows: list) -> dict | None:
    if not rows:
        return None
    best = max(rows, key=lambda r: (r['score'], -r['total']))
    return {k: best[k] for k in ('score', 'total', 'createdAt')}


@bp.get('/quiz/questions')
@require_auth
def list_questions():
    """?group=<id>&count= 按所选用户组的抽题机制抽取（默认 10，上限 20）。

    group 省略时使用公共题库；group 必须是本人所在的组。
    quizMode：group 只用组内；fallback 组内为空回退公共；mixed 组内与公共混合。
    """
    try:
        count = max(1, min(int(request.args.get('count', 10)), MAX_QUESTIONS))
    except ValueError:
        count = 10
    group_id = request.args.get('group')
    group = None
    if group_id:
        group = query_one(
            'SELECT g.id, g.name, g.quizMode FROM group_members gm JOIN groups g ON g.id = gm.groupId '
            "WHERE gm.userId = ? AND gm.role = 'member' AND gm.groupId = ?",
            (g.user['id'], group_id),
        )
        if not group:
            raise forbidden('仅可玩自己所在组的题库')
        pool = [question_view(r) for r in query_all('SELECT * FROM quiz_questions WHERE groupId = ?', (group_id,))]
        if group['quizMode'] == 'mixed':
            pool += [question_view(r) for r in query_all('SELECT * FROM quiz_questions WHERE groupId IS NULL')]
        elif group['quizMode'] == 'fallback' and not pool:
            pool = [question_view(r) for r in query_all('SELECT * FROM quiz_questions WHERE groupId IS NULL')]
    else:
        pool = [question_view(r) for r in query_all('SELECT * FROM quiz_questions WHERE groupId IS NULL')]
    total = len(pool)
    if total > count:
        pool = random.sample(pool, count)
    return jsonify({
        'items': pool,
        'total': total,
        'group': {'id': group['id'], 'name': group['name']} if group else None,
    })


@bp.post('/quiz/attempts')
@require_auth
def create_attempt():
    """记录一局成绩，返回本次记录与历史最佳。"""
    body = request.get_json(silent=True) or {}
    score, total = body.get('score'), body.get('total')
    if not isinstance(score, int) or not isinstance(total, int) or not 0 <= score <= total:
        raise bad_request('score 与 total 必须为整数，且 0 ≤ score ≤ total')
    attempt = {'id': gen_id('qa'), 'userId': g.user['id'], 'score': score, 'total': total, 'createdAt': now_iso()}
    execute(
        'INSERT INTO quiz_attempts (id, userId, score, total, createdAt) VALUES (?, ?, ?, ?, ?)',
        tuple(attempt.values()),
    )
    commit()
    rows = query_all('SELECT * FROM quiz_attempts WHERE userId = ?', (g.user['id'],))
    return jsonify({'attempt': attempt, 'best': best_view(rows)}), 201


@bp.get('/quiz/stats')
@require_auth
def quiz_stats():
    rows = query_all('SELECT * FROM quiz_attempts WHERE userId = ? ORDER BY createdAt DESC', (g.user['id'],))
    return jsonify({
        'attempts': len(rows),
        'best': best_view(rows),
        'last': {k: rows[0][k] for k in ('score', 'total', 'createdAt')} if rows else None,
    })
