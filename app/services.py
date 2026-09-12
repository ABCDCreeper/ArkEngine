"""共享业务逻辑：权限校验、资源视图、任务状态变更联动（契约 2.x / 3.x）。"""
import json
import random

from flask import request

from .db import bump_revision, execute, gen_id, get_db, json_loads, now_iso, query_all, query_one
from .errors import ApiError, forbidden

TASK_STATUSES = ('todo', 'doing', 'review', 'done')
TASK_STATUS_LABEL = {'todo': '待认领', 'doing': '进行中', 'review': '待验收', 'done': '已完成'}

# 角色层级：数值越大权限越高。管理范围一律按「严格低于自己」计算，前端也共用同一顺序。
ROLE_RANK = {'student': 0, 'teacher': 1, 'schooladmin': 2, 'admin': 3, 'superadmin': 4}

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
    ensure_active(project_id)


def begin_write() -> None:
    # Serialize read/validate/write so concurrent claims and closure cannot race.
    if not get_db().in_transaction:
        execute('BEGIN IMMEDIATE')


def ensure_active(project_id: str) -> None:
    begin_write()
    if get_project_or_404(project_id)['status'] != 'active':
        raise ApiError(409, 'PROJECT_FINISHED', '项目已结题，过程记录只读')


def is_teacher_tier(user: dict) -> bool:
    """教师或管理角色（teacher / schooladmin / admin / superadmin）。"""
    return user['role'] in ('teacher', 'schooladmin', 'admin', 'superadmin')


def can_review(project: dict, user: dict) -> bool:
    """能否验收/结题该项目（单项目场景，只查必要的一行）。

    与批量版本 reviewable_project_ids 共用同一条规则 may_review，
    因此两者不会因实现不同而给出不同答案。
    """
    group_school = None
    if project.get('groupId'):
        row = query_one('SELECT schoolId FROM groups WHERE id = ?', (project['groupId'],))
        group_school = row['schoolId'] if row else None
    return may_review(project, group_school, review_scope(user))


def reviewable_project_ids(user: dict) -> set[str] | None:
    """我可验收的项目 id 集合；返回 None 表示不受限制（平台管理员）。

    用于列表/聚合场景：一次取回项目与分组归属，再用同一条规则 may_review 筛选，
    避免为每个项目各发一次查询。
    """
    scope = review_scope(user)
    if scope['kind'] == 'none':
        return set()
    if scope['kind'] == 'admin' and scope['school'] is None:
        return None
    rows = query_all('SELECT p.id, p.groupId, g.schoolId AS groupSchool FROM projects p '
                     'LEFT JOIN groups g ON g.id = p.groupId')
    return {r['id'] for r in rows if may_review(r, r['groupSchool'], scope)}


def review_scope(user: dict) -> dict:
    """复审权限所需的上下文（学校范围、负责的组），一次取出供多处复用。"""
    role = user['role']
    if role in ('schooladmin', 'admin', 'superadmin'):
        return {'kind': 'admin', 'school': school_scope(user)}
    if role == 'teacher':
        rows = query_all("SELECT groupId FROM group_members WHERE userId = ? AND role = 'teacher'",
                         (user['id'],))
        return {'kind': 'teacher', 'groups': {r['groupId'] for r in rows}}
    return {'kind': 'none'}


def may_review(project: dict, group_school: str | None, scope: dict) -> bool:
    """纯函数形式的复审判断，便于批量组装时避免逐个查询。"""
    kind = scope['kind']
    if kind == 'admin':
        limit = scope['school']
        if limit is None:
            return True
        # 未归属学校的项目（无分组或分组未归属）保持可见，避免收紧后管理员看不到历史数据
        return group_school in (None, limit)
    if kind == 'teacher':
        return project['groupId'] is None or project['groupId'] in scope['groups']
    return False


# ---------------------------------------------------------------- 学校归属

def school_scope(user: dict) -> str | None:
    """调用者的学校归属。为空表示不受学校约束（平台管理员，或升级前的既有账号）。

    校管理员只应管理本校数据；平台管理员与未归属数据保持全局可见，避免旧库迁移后失权。
    """
    row = query_one('SELECT schoolId FROM users WHERE id = ?', (user['id'],))
    return row['schoolId'] if row else None


def can_manage_group(group: dict, user: dict) -> bool:
    """管理角色按学校范围可管理分组；教师需为该组负责老师。"""
    role = user['role']
    if role in ('schooladmin', 'admin', 'superadmin'):
        scope = school_scope(user)
        return scope is None or group['schoolId'] in (None, scope)
    if role != 'teacher':
        return False
    return bool(query_one(
        "SELECT 1 FROM group_members WHERE groupId = ? AND userId = ? AND role = 'teacher'",
        (group['id'], user['id']),
    ))


def visible_group_ids(user: dict) -> list[str]:
    """管理角色可见的分组 id；超出学校范围的分组不返回。"""
    if user['role'] not in ('schooladmin', 'admin', 'superadmin'):
        return []
    scope = school_scope(user)
    if scope is None:
        rows = query_all('SELECT id FROM groups')
    else:
        rows = query_all('SELECT id FROM groups WHERE schoolId IS NULL OR schoolId = ?', (scope,))
    return [r['id'] for r in rows]


def ensure_read_access(project_id: str, user: dict) -> None:
    """读操作：教师/管理角色、项目成员、公共项目、同组成员可读（组间隔离）。"""
    if can_review(get_project_or_404(project_id), user):
        return
    if query_one('SELECT 1 FROM members WHERE projectId = ? AND userId = ?', (project_id, user['id'])):
        return
    project = query_one('SELECT groupId FROM projects WHERE id = ?', (project_id,))
    if not project or project['groupId'] is None:
        return
    if query_one(
        "SELECT 1 FROM group_members WHERE groupId = ? AND userId = ? AND role = 'member'",
        (project['groupId'], user['id']),
    ):
        return
    raise forbidden('仅本组成员可访问该项目')


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


def build_project_view(project: dict, user: dict, topic: dict | None, group: dict | None,
                       members: list[dict], progress: dict, reviewing: bool) -> dict:
    """由已取好的关联数据组装项目视图（单个与批量共用，避免两处权限逻辑分叉）。"""
    active = project['status'] == 'active'
    editable = active and user['role'] == 'student' and any(m['id'] == user['id'] for m in members)
    return {
        **project,
        'topic': {'id': topic['id'], 'title': topic['title'], 'subjects': json_loads(topic['subjects'])}
        if topic else None,
        'group': group,
        'members': members,
        'progress': progress,
        'permissions': {'edit': editable, 'review': active and reviewing,
                        'finish': active and (reviewing or (editable and project['leaderId'] == user['id'])),
                        'annotate': reviewing},
    }


def project_view(project: dict) -> dict:
    """单个项目视图：附加 topic 摘要、所属组、成员列表与实时进度（契约 2.3）。"""
    topic = query_one('SELECT id, title, subjects FROM topics WHERE id = ?', (project['topicId'],))
    group_row = query_one('SELECT id, name, schoolId FROM groups WHERE id = ?', (project['groupId'],)) \
        if project.get('groupId') else None
    group = {'id': group_row['id'], 'name': group_row['name']} if group_row else None
    from flask import g as request_context
    user = request_context.user
    reviewing = may_review(project, group_row['schoolId'] if group_row else None, review_scope(user))
    return build_project_view(project, user, topic, group, project_members(project['id']),
                              project_progress(project['id']), reviewing)


def project_views(projects: list[dict], user: dict) -> list[dict]:
    """批量组装项目视图。

    逐个调用 project_view 会对每个项目发出约 5 次查询（topic、group、成员、进度、权限），
    列表接口因此构成 N+1。这里按类型各查一次，用 IN 一次性取回。
    """
    if not projects:
        return []
    ids = [p['id'] for p in projects]
    marks = ','.join('?' * len(ids))

    topics = {t['id']: t for t in query_all(
        f'SELECT id, title, subjects FROM topics WHERE id IN ({marks})', ids)}

    group_ids = [p['groupId'] for p in projects if p.get('groupId')]
    groups: dict[str, dict] = {}
    if group_ids:
        gmarks = ','.join('?' * len(group_ids))
        groups = {g['id']: g for g in query_all(
            f'SELECT id, name, schoolId FROM groups WHERE id IN ({gmarks})', group_ids)}

    members_by_project: dict[str, list[dict]] = {}
    for row in query_all(
        f'SELECT m.projectId, u.id, u.username, u.name, u.role FROM members m '
        f'JOIN users u ON u.id = m.userId WHERE m.projectId IN ({marks})', ids):
        members_by_project.setdefault(row.pop('projectId'), []).append(row)

    progress_by_project: dict[str, dict] = {}
    for row in query_all(f'SELECT projectId, status FROM tasks WHERE projectId IN ({marks})', ids):
        entry = progress_by_project.setdefault(row['projectId'], {'done': 0, 'total': 0})
        entry['total'] += 1
        if row['status'] == 'done':
            entry['done'] += 1

    scope = review_scope(user)
    views = []
    for project in projects:
        group_row = groups.get(project['groupId']) if project.get('groupId') else None
        reviewing = may_review(project, group_row['schoolId'] if group_row else None, scope)
        views.append(build_project_view(
            project, user, topics.get(project['topicId']),
            {'id': group_row['id'], 'name': group_row['name']} if group_row else None,
            members_by_project.get(project['id'], []),
            progress_by_project.get(project['id'], {'done': 0, 'total': 0}),
            reviewing,
        ))
    return views


def task_view(task: dict) -> dict:
    """任务视图：补上验收人姓名。

    负责教师不在项目成员表里，前端无法把 verifiedBy 解析成姓名，
    因此由服务端 join 出来（列表接口已在 SQL 中直接 join，这里只兜单个任务响应）。
    """
    if task.get('verifiedBy') and not task.get('verifiedByName'):
        row = query_one('SELECT name FROM users WHERE id = ?', (task['verifiedBy'],))
        task['verifiedByName'] = row['name'] if row else None
    else:
        task.setdefault('verifiedByName', None)
    # 与列表接口保持同一形状：任务维度的过程证据（关联专注时长、同伴互评数）
    if 'focusMinutes' not in task:
        task['focusMinutes'] = query_one(
            "SELECT COALESCE(SUM(durationMin), 0) AS m FROM focus_sessions "
            "WHERE taskId = ? AND type = 'focus'", (task['id'],))['m']
    if 'peerReviewCount' not in task:
        task['peerReviewCount'] = query_one(
            'SELECT COUNT(*) AS c FROM task_reviews WHERE taskId = ?', (task['id'],))['c']
    return task


def touch_project(project_id: str) -> None:
    execute('UPDATE projects SET updatedAt = ? WHERE id = ?', (now_iso(), project_id))
    # 过程记录一有写入就递增修订号，前端据此判断是否需要重新拉取
    bump_revision(project_id)


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


def criteria_summary(raw: str | None) -> str | None:
    """验收标准达标情况，如「2/3 项达标」；没有清单时返回 None。"""
    if not raw:
        return None
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(items, list) or not items:
        return None
    done = sum(1 for item in items if isinstance(item, dict) and item.get('done') is True)
    return f'{done}/{len(items)} 项达标'


def handle_task_status_change(task: dict, user: dict, old_status: str) -> None:
    """任务状态变更联动（契约 3.4）：追加动态；变为 done 时打卡 + 里程碑反馈。

    三个写操作在同一个 sqlite 事务中，由路由统一 commit。
    """
    if old_status == task['status']:
        return
    detail = f"状态更新为 {TASK_STATUS_LABEL[task['status']]}"
    if task['status'] == 'done':
        summary = criteria_summary(task.get('criteria'))
        # 验收标准是否逐条核对过，决定了这次验收有多少依据可查
        detail += f"（验收标准 {summary}）" if summary else '（任务未设验收标准）'
    add_task_log(task['projectId'], task['id'], user, 'status', detail)
    if task['status'] == 'done' and old_status != 'done':
        execute(
            'INSERT INTO checkins (id, projectId, userId, content, createdAt) VALUES (?, ?, ?, ?, ?)',
            (gen_id('c'), task['projectId'], task['assigneeId'], f"完成里程碑任务「{task['title']}」", now_iso()),
        )
        add_feedback(task['projectId'], user, 'milestone', random.choice(FEEDBACK_POOL))


def pick_feedback() -> str:
    return random.choice(FEEDBACK_POOL)


def add_audit(actor: dict, action: str, target_type: str, target_id: str, detail: str = '') -> None:
    """记录一条管理操作审计（改角色、重置口令、删除账号或分组、结题等）。

    只记录"谁对什么做了什么"，不记录口令等敏感内容。
    """
    execute(
        'INSERT INTO audit_logs (id, actorId, action, targetType, targetId, detail, createdAt) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (gen_id('al'), actor['id'], action, target_type, target_id, detail, now_iso()),
    )


# ---------------------------------------------------------------- 分页

def page_params() -> tuple[int, int, int]:
    """从查询串解析 (page, pageSize, offset)，默认 pageSize=100，上限 200。"""
    try:
        page = max(int(request.args.get('page', 1)), 1)
    except ValueError:
        page = 1
    try:
        page_size = min(max(int(request.args.get('pageSize', 100)), 1), 200)
    except ValueError:
        page_size = 100
    return page, page_size, (page - 1) * page_size


def paged(items: list) -> dict:
    """内存分页：适用于本来就要全量读取、或需要跨表聚合后再排序的列表。"""
    page, page_size, start = page_params()
    return {'items': items[start:start + page_size], 'total': len(items), 'page': page, 'pageSize': page_size}


def paged_query(sql: str, args: list) -> dict:
    """SQL 层分页：总数用 COUNT 子查询，明细用 LIMIT/OFFSET。

    列表数据量增长后，先全量取出再切片会白白读整表；这里把分页下推到数据库，
    响应格式与 paged() 保持一致。
    """
    page, page_size, offset = page_params()
    total = query_one(f'SELECT COUNT(*) AS c FROM ({sql})', args)['c']
    items = query_all(f'{sql} LIMIT ? OFFSET ?', [*args, page_size, offset])
    return {'items': items, 'total': total, 'page': page, 'pageSize': page_size}
