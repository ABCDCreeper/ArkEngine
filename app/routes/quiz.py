"""知识闯关路由（契约 3.10）：随机抽题、成绩记录与统计。"""
import random

from flask import Blueprint, g, jsonify, request

from ..auth import require_auth
from ..db import commit, execute, gen_id, json_loads, now_iso, query_all
from ..errors import bad_request

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
    """?count= 随机抽题数量（默认 10，上限 20）。"""
    try:
        count = max(1, min(int(request.args.get('count', 10)), MAX_QUESTIONS))
    except ValueError:
        count = 10
    all_items = [question_view(r) for r in query_all('SELECT * FROM quiz_questions')]
    total = len(all_items)
    if total > count:
        items = random.sample(all_items, count)
    else:
        items = all_items
    return jsonify({'items': items, 'total': total})


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
