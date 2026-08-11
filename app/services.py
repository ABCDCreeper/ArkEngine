"""共享业务逻辑：权限校验、资源视图、任务状态变更联动（契约 2.x / 3.x）。"""
import random

from flask import request

from .db import execute, gen_id, json_loads, now_iso, query_all, query_one
from .errors import ApiError, forbidden

TASK_STATUSES = ('todo', 'doing', 'review', 'done')
TASK_STATUS_LABEL = {'todo': '待认领', 'doing': '进行中', 'review': '待验收', 'done': '已完成'}

# 系统反馈自动生成语料（契约第 4 节）
FEEDBACK_POOL = [
    '里程碑达成！你们把一个大目标拆成了可执行的小步，这正是工程师思维。',
    '干得漂亮！这一步的完成意味着整个项目又向前推进了一截。',
    '进度同步得很好，接下来可以尝试把成果整理成可视化材料。',
    '团队协作满分！记得在打卡里记录下这次尝试中的收获与踩坑。',
    '这个节点很关键，完成后建议做一次小复盘，把经验沉淀到档案里。',
    '思路清晰，继续推进！遇到瓶颈时回到星云看板看看最初的想法。',
]


# ---------------------------------------------------------------- 基础查询

def get_project_or_404(project_id: str) -> dict:
    project = query_one('SELECT * FROM projects WHERE id = ?', (project_id,))
    if not project:
        raise ApiError(404, 'PROJECT_NOT_FOUND', '项目不存在')
    return project


def get_task_or_404(task_id: str) -> dict:
    task = query_one('SELECT * FROM tasks WHERE id = ?', (task_id,))
    if not task:
        raise ApiError(404, 'TASK_NOT_FOUND', '任务不存在')
    return task


def member_of(project_id: str, user: dict) -> None:
    """写操作：必须是项目成员（学生），教师对协作内容只读。"""
    if not query_one('SELECT 1 FROM members WHERE projectId = ? AND userId = ?', (project_id, user['id'])):
        raise forbidden('仅项目成员可执行此操作')


def ensure_read_access(project_id: str, user: dict) -> None:
    """读操作：学生需为成员，教师可读全部项目。"""
    if user['role'] != 'teacher':
        member_of(project_id, user)


# ---------------------------------------------------------------- 视图组装

def user_brief(user: dict) -> dict:
    return {k: v for k, v in user.items() if k != 'password'}


def topic_view(topic: dict) -> dict:
    return {**topic, 'subjects': json_loads(topic['subjects']), 'tags': json_loads(topic['tags'])}


def resource_view(resource: dict) -> dict:
    return {**resource, 'tags': json_loads(resource['tags'])}


def project_members(project_id: str) -> list[dict]:
    rows = query_all(
        'SELECT u.id, u.username, u.name, u.role FROM members m JOIN users u ON u.id = m.userId '
        'WHERE m.projectId = ?',
        (project_id,),
    )
    return rows


def project_progress(project_id: str) -> dict:
    rows = query_all('SELECT status FROM tasks WHERE projectId = ?', (project_id,))
    return {'done': sum(1 for t in rows if t['status'] == 'done'), 'total': len(rows)}


def project_view(project: dict) -> dict:
    """Project 视图：附加 topic 摘要、成员列表与实时进度（契约 2.3）。"""
    topic = query_one('SELECT id, title, subjects FROM topics WHERE id = ?', (project['topicId'],))
    return {
        **project,
        'topic': {'id': topic['id'], 'title': topic['title'], 'subjects': json_loads(topic['subjects'])}
        if topic else None,
        'members': project_members(project['id']),
        'progress': project_progress(project['id']),
    }


def touch_project(project_id: str) -> None:
    execute('UPDATE projects SET updatedAt = ? WHERE id = ?', (now_iso(), project_id))


# ---------------------------------------------------------------- 联动写入

def add_task_log(project_id: str, task_id: str, user: dict, action: str, detail: str) -> None:
    execute(
        'INSERT INTO task_logs (id, projectId, taskId, userId, action, detail, createdAt) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (gen_id('l'), project_id, task_id, user['id'], action, detail, now_iso()),
    )


def add_feedback(project_id: str, user: dict, ftype: str, content: str) -> None:
    execute(
        'INSERT INTO feedbacks (id, projectId, userId, type, content, createdAt) VALUES (?, ?, ?, ?, ?, ?)',
        (gen_id('f'), project_id, user['id'], ftype, content, now_iso()),
    )


def handle_task_status_change(task: dict, user: dict, old_status: str) -> None:
    """任务状态变更联动（契约 3.4）：追加动态；变为 done 时打卡 + 里程碑反馈。

    三个写操作在同一个 sqlite 事务中，由路由统一 commit。
    """
    add_task_log(task['projectId'], task['id'], user, 'status', f"状态更新为 {TASK_STATUS_LABEL[task['status']]}")
    if task['status'] == 'done' and old_status != 'done':
        execute(
            'INSERT INTO checkins (id, projectId, userId, content, createdAt) VALUES (?, ?, ?, ?, ?)',
            (gen_id('c'), task['projectId'], user['id'], f"完成里程碑任务「{task['title']}」", now_iso()),
        )
        add_feedback(task['projectId'], user, 'milestone', random.choice(FEEDBACK_POOL))


def pick_feedback() -> str:
    return random.choice(FEEDBACK_POOL)


# ---------------------------------------------------------------- 分页

def paged(items: list) -> dict:
    """列表响应格式（契约 1.5）：默认 pageSize=100，支持 ?page= / ?pageSize=。"""
    try:
        page = max(int(request.args.get('page', 1)), 1)
    except ValueError:
        page = 1
    try:
        page_size = min(max(int(request.args.get('pageSize', 100)), 1), 200)
    except ValueError:
        page_size = 100
    start = (page - 1) * page_size
    return {'items': items[start:start + page_size], 'total': len(items), 'page': page, 'pageSize': page_size}
