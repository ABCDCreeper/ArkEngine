"""知识闯关路由（契约 3.10）：随机抽题、成绩记录与统计。"""
import random
import json
from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request

from ..auth import require_auth
from ..db import commit, execute, gen_id, iso, json_dumps, json_loads, now_iso, query_all, query_one
from ..errors import ApiError, bad_request, forbidden, get_json_body
from ..services import begin_write

bp = Blueprint('quiz', __name__)

MAX_QUESTIONS = 20

# 错题复习间隔（天）：答对逐级推进，走完三轮且全对即视为已掌握
REVIEW_INTERVALS = (3, 7, 14)


def next_review_at(stage: int, moment: datetime | None = None) -> str:
    base = moment or datetime.now(timezone.utc)
    days = REVIEW_INTERVALS[min(stage, len(REVIEW_INTERVALS) - 1)]
    return iso(base + timedelta(days=days))


def record_wrong(user_id: str, question_id: str, choice: int) -> None:
    """答错即入错题本；再次答错会把进度退回到第一轮。"""
    now = now_iso()
    execute(
        'INSERT INTO quiz_wrong_answers (id, userId, questionId, choice, stage, nextReviewAt, createdAt) '
        'VALUES (?, ?, ?, ?, 0, ?, ?) '
        'ON CONFLICT(userId, questionId) DO UPDATE SET '
        'choice = excluded.choice, stage = 0, nextReviewAt = excluded.nextReviewAt, resolvedAt = NULL',
        (gen_id('wa'), user_id, question_id, choice, next_review_at(0), now),
    )


def question_view(row: dict) -> dict:
    return {**row, 'options': json_loads(row['options'])}


def best_view(rows: list) -> dict | None:
    if not rows:
        return None
    best = max(rows, key=lambda r: (r['score'] / r['total'] if r['total'] else 0, r['total']))
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
    round_id = None
    if pool:
        round_id = gen_id('qr')
        execute('DELETE FROM quiz_rounds WHERE expiresAt < ?', (now_iso(),))
        execute(
            'INSERT INTO quiz_rounds (id, userId, questions, createdAt, expiresAt) VALUES (?, ?, ?, ?, ?)',
            (round_id, g.user['id'], json_dumps(pool), now_iso(),
             iso(datetime.now(timezone.utc) + timedelta(hours=2))),
        )
        commit()
    return jsonify({
        'items': [{k: v for k, v in q.items() if k not in ('answer', 'explanation')} for q in pool],
        'roundId': round_id,
        'total': total,
        'group': {'id': group['id'], 'name': group['name']} if group else None,
    })


def owned_round(round_id):
    if not isinstance(round_id, str) or not round_id:
        raise bad_request('缺少有效的 roundId')
    begin_write()
    row = query_one('SELECT * FROM quiz_rounds WHERE id = ? AND userId = ?', (round_id, g.user['id']))
    if not row:
        raise ApiError(404, 'ROUND_NOT_FOUND', '本局不存在，请重新开始')
    if row['expiresAt'] < now_iso():
        raise ApiError(409, 'ROUND_EXPIRED', '本局已过期，请重新开始')
    return row


@bp.post('/quiz/rounds/<round_id>/answers')
@require_auth
def answer_question(round_id):
    body = get_json_body()
    row = owned_round(round_id)
    questions = json.loads(row['questions'])
    answers = json.loads(row['answers'])
    question = next((q for q in questions if q['id'] == body.get('questionId')), None)
    choice = body.get('choice')
    if not question or type(choice) is not int or not 0 <= choice < len(question['options']):
        raise bad_request('题目或选项无效')
    qid = question['id']
    if qid in answers:
        if answers[qid] != choice:
            raise ApiError(409, 'ANSWER_LOCKED', '已提交的答案不能修改')
    else:
        if query_one('SELECT 1 FROM quiz_attempts WHERE id = ?', (round_id,)):
            raise ApiError(409, 'ROUND_FINISHED', '本局已完成')
        answers[qid] = choice
        execute('UPDATE quiz_rounds SET answers = ? WHERE id = ?', (json_dumps(answers), round_id))
        if choice != question['answer']:
            record_wrong(g.user['id'], qid, choice)
    commit()
    return jsonify({'answer': question['answer'], 'explanation': question['explanation'],
                    'correct': choice == question['answer'],
                    'score': sum(10 for q in questions if answers.get(q['id']) == q['answer'])})


@bp.post('/quiz/attempts')
@require_auth
def create_attempt():
    """记录一局成绩，返回本次记录与历史最佳。"""
    body = get_json_body()
    if set(body) != {'roundId'}:
        raise bad_request('只允许提交 roundId，成绩由服务器计算')
    row = owned_round(body.get('roundId'))
    questions = json.loads(row['questions'])
    answers = json.loads(row['answers'])
    if len(answers) != len(questions):
        raise ApiError(409, 'ROUND_INCOMPLETE', '请先完成本局全部题目')
    attempt = query_one('SELECT * FROM quiz_attempts WHERE id = ?', (row['id'],))
    status = 200 if attempt else 201
    if not attempt:
        attempt = {'id': row['id'], 'userId': g.user['id'],
                   'score': sum(10 for q in questions if answers[q['id']] == q['answer']),
                   'total': len(questions) * 10, 'createdAt': now_iso()}
        execute(
            'INSERT INTO quiz_attempts (id, userId, score, total, createdAt) VALUES (?, ?, ?, ?, ?)',
            tuple(attempt.values()),
        )
    commit()
    rows = query_all('SELECT * FROM quiz_attempts WHERE userId = ?', (g.user['id'],))
    return jsonify({'attempt': attempt, 'best': best_view(rows)}), status


@bp.get('/quiz/stats')
@require_auth
def quiz_stats():
    rows = query_all('SELECT * FROM quiz_attempts WHERE userId = ? ORDER BY createdAt DESC', (g.user['id'],))
    due = query_one(
        'SELECT COUNT(*) AS c FROM quiz_wrong_answers '
        'WHERE userId = ? AND resolvedAt IS NULL AND nextReviewAt <= ?', (g.user['id'], now_iso()))['c']
    open_total = query_one(
        'SELECT COUNT(*) AS c FROM quiz_wrong_answers WHERE userId = ? AND resolvedAt IS NULL',
        (g.user['id'],))['c']
    return jsonify({
        'attempts': len(rows),
        'best': best_view(rows),
        'last': {k: rows[0][k] for k in ('score', 'total', 'createdAt')} if rows else None,
        'wrong': {'open': open_total, 'due': due},
    })


@bp.get('/quiz/wrong-answers')
@require_auth
def list_wrong_answers():
    """我的错题本：?due=1 只看今天该复习的（契约 3.10）。

    返回题目内容但不含答案与解析——答案只在提交复习结果时返回，
    与正常答题保持一致，避免直接读到答案。
    """
    only_due = request.args.get('due') in ('1', 'true')
    sql = ('SELECT w.id, w.questionId, w.choice, w.stage, w.nextReviewAt, w.createdAt, w.resolvedAt, '
           'q.category, q.difficulty, q.question, q.options '
           'FROM quiz_wrong_answers w JOIN quiz_questions q ON q.id = w.questionId '
           'WHERE w.userId = ?')
    args = [g.user['id']]
    if only_due:
        sql += ' AND w.resolvedAt IS NULL AND w.nextReviewAt <= ?'
        args.append(now_iso())
    sql += ' ORDER BY w.resolvedAt IS NOT NULL, w.nextReviewAt'
    items = [
        {**row, 'options': json_loads(row['options']),
         'stageLabel': ('已掌握' if row['resolvedAt']
                        else f'第 {row["stage"] + 1}/{len(REVIEW_INTERVALS)} 轮')}
        for row in query_all(sql, args)
    ]
    return jsonify({'items': items, 'total': len(items)})


@bp.post('/quiz/wrong-answers/<question_id>/review')
@require_auth
def review_wrong_answer(question_id):
    """复习一道错题：答对推进一轮，答错退回第一轮。"""
    body = get_json_body()
    choice = body.get('choice')
    entry = query_one('SELECT * FROM quiz_wrong_answers WHERE userId = ? AND questionId = ?',
                      (g.user['id'], question_id))
    if not entry:
        raise ApiError(404, 'NOT_FOUND', '该题不在你的错题本中')
    question = query_one('SELECT * FROM quiz_questions WHERE id = ?', (question_id,))
    if not question:
        raise ApiError(404, 'NOT_FOUND', '题目已不存在')
    options = json_loads(question['options'])
    if type(choice) is not int or not 0 <= choice < len(options):
        raise bad_request('选项无效')
    correct = choice == question['answer']
    if correct:
        stage = entry['stage'] + 1
        resolved = stage >= len(REVIEW_INTERVALS)
        execute('UPDATE quiz_wrong_answers SET stage = ?, nextReviewAt = ?, resolvedAt = ? WHERE id = ?',
                (min(stage, len(REVIEW_INTERVALS)), next_review_at(stage), now_iso() if resolved else None,
                 entry['id']))
    else:
        record_wrong(g.user['id'], question_id, choice)
        stage, resolved = 0, False
    commit()
    current = query_one('SELECT * FROM quiz_wrong_answers WHERE id = ?', (entry['id'],))
    return jsonify({
        'correct': correct, 'answer': question['answer'], 'explanation': question['explanation'],
        'stage': current['stage'], 'resolved': bool(current['resolvedAt']),
        'nextReviewAt': current['nextReviewAt'], 'totalStages': len(REVIEW_INTERVALS),
    })
