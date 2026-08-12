"""SQLite 数据层：连接管理、建表、种子数据。

表结构直接对应 docs/api.md 第 2 节数据模型，列名采用 camelCase，
与 JSON 契约字段一一对应（users 表除外，password 仅服务端使用）。
"""
import json
import random
import secrets
import sqlite3
import string
from datetime import datetime, timedelta, timezone

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password TEXT NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('student', 'teacher'))
);
CREATE TABLE IF NOT EXISTS topics (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  subjects TEXT NOT NULL DEFAULT '[]',
  tags TEXT NOT NULL DEFAULT '[]',
  difficulty TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  topicId TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'finished')),
  inviteCode TEXT NOT NULL UNIQUE,
  leaderId TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  createdAt TEXT NOT NULL,
  updatedAt TEXT NOT NULL,
  finishedAt TEXT
);
CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY,
  projectId TEXT NOT NULL,
  userId TEXT NOT NULL,
  joinedAt TEXT NOT NULL,
  UNIQUE (projectId, userId)
);
CREATE TABLE IF NOT EXISTS mind_nodes (
  id TEXT PRIMARY KEY,
  projectId TEXT NOT NULL,
  parentId TEXT,
  label TEXT NOT NULL,
  createdAt TEXT NOT NULL,
  updatedAt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
  id TEXT PRIMARY KEY,
  projectId TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  color TEXT NOT NULL DEFAULT '#fde68a',
  x INTEGER NOT NULL DEFAULT 20,
  y INTEGER NOT NULL DEFAULT 20,
  createdAt TEXT NOT NULL,
  updatedAt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  projectId TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  assigneeId TEXT,
  status TEXT NOT NULL CHECK (status IN ('todo', 'doing', 'review', 'done')),
  dueDate TEXT,
  createdAt TEXT NOT NULL,
  updatedAt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_logs (
  id TEXT PRIMARY KEY,
  projectId TEXT NOT NULL,
  taskId TEXT NOT NULL,
  userId TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL,
  createdAt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkins (
  id TEXT PRIMARY KEY,
  projectId TEXT NOT NULL,
  userId TEXT NOT NULL,
  content TEXT NOT NULL,
  createdAt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedbacks (
  id TEXT PRIMARY KEY,
  projectId TEXT NOT NULL,
  userId TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('milestone', 'guide')),
  content TEXT NOT NULL,
  createdAt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resources (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  category TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS annotations (
  id TEXT PRIMARY KEY,
  projectId TEXT NOT NULL,
  userId TEXT NOT NULL,
  content TEXT NOT NULL,
  createdAt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS focus_sessions (
  id TEXT PRIMARY KEY,
  userId TEXT NOT NULL,
  durationMin INTEGER NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('focus', 'break')),
  createdAt TEXT NOT NULL
);
-- 服务端会话（登录签发，登出删除，实现 token 失效）
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  userId TEXT NOT NULL,
  token TEXT NOT NULL UNIQUE,
  createdAt TEXT NOT NULL
);
"""


# ---------------------------------------------------------------- 时间与 ID

def iso(dt: datetime) -> str:
    """转 JS 兼容的 ISO 8601 字符串（UTC，微秒精度，Z 结尾）。

    微秒精度保证同一进程内连续写入的时间戳严格递增，
    使「按时间倒序」的列表排序稳定（毫秒精度在同一毫秒内可能相同）。
    """
    return dt.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def days_ago(days: int, hour: int = 10, minute: int = 0) -> str:
    """n 天前某时刻的 ISO 字符串（种子数据用）。"""
    d = datetime.now(timezone.utc) - timedelta(days=days)
    return iso(d.replace(hour=hour, minute=minute, second=0, microsecond=0))


def gen_id(prefix: str) -> str:
    """生成运行时新资源 ID：前缀 + 6 位随机字符（如 p3f9k2a）。"""
    return prefix + ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))


# ---------------------------------------------------------------- 连接管理

def get_db() -> sqlite3.Connection:
    if 'db' not in g:
        db = sqlite3.connect(current_app.config['DATABASE'])
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        g.db = db
    return g.db


def close_db(_exc=None):
    db = g.pop('db', None)
    if db is not None:
        # 未 commit 的事务（请求出错时）随连接关闭自动回滚
        db.close()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)


def query_all(sql: str, args=()) -> list[dict]:
    return [dict(r) for r in get_db().execute(sql, args).fetchall()]


def query_one(sql: str, args=()) -> dict | None:
    row = get_db().execute(sql, args).fetchone()
    return dict(row) if row else None


def execute(sql: str, args=()) -> int:
    return get_db().execute(sql, args).lastrowid


def commit():
    get_db().commit()


# ---------------------------------------------------------------- 种子数据

FEEDBACK_POOL = [
    '里程碑达成！你们把一个大目标拆成了可执行的小步，这正是工程师思维。',
    '干得漂亮！这一步的完成意味着整个项目又向前推进了一截。',
    '进度同步得很好，接下来可以尝试把成果整理成可视化材料。',
    '团队协作满分！记得在打卡里记录下这次尝试中的收获与踩坑。',
    '这个节点很关键，完成后建议做一次小复盘，把经验沉淀到档案里。',
    '思路清晰，继续推进！遇到瓶颈时回到星云看板看看最初的想法。',
]

_seed_users = [
    ('u1', 'student', '123456', '张三', 'student'),
    ('u2', 'student2', '123456', '李四', 'student'),
    ('u3', 'student3', '123456', '王五', 'student'),
    ('u4', 'student4', '123456', '赵六', 'student'),
    ('t1', 'teacher', '123456', '王老师', 'teacher'),
]

_seed_topics = [
    ('topic1', '火星基地能源方案设计', '为火星基地设计可持续能源系统，比较太阳能、核能与风能的组合方案，输出能量平衡计算与架构图。',
     ['物理', '工程'], ['能源', '太空'], '挑战'),
    ('topic2', '校园智能垃圾分类助手', '设计一款面向校园的智能垃圾分类工具，结合图像识别与科普互动，完成原型与演示。',
     ['编程', '环保'], ['AI', '物联网'], '进阶'),
    ('topic3', '星舰生命维持系统', '模拟星舰内生命维持系统：氧气循环、水循环与温控，构建系统模型并评估可靠性。',
     ['生物', '工程'], ['生命科学', '系统'], '进阶'),
    ('topic4', '声波可视化艺术装置', '将声音信号实时转化为可视化图案，结合物理原理与艺术表达，制作交互装置。',
     ['物理', '艺术'], ['声学', '交互'], '入门'),
]

_seed_projects = [
    ('p1', 'topic1', '火星基地能源方案', 'active', 'P1-7F3A', 'u1', days_ago(12), days_ago(0, 9), None),
    ('p2', 'topic2', '校园智能垃圾分类助手', 'finished', 'P2-9B1C', 'u1', days_ago(40), days_ago(6, 16), days_ago(6, 17)),
]

_seed_members = [
    ('m1', 'p1', 'u1', days_ago(12)), ('m2', 'p1', 'u2', days_ago(11)), ('m3', 'p1', 'u3', days_ago(10)),
    ('m4', 'p2', 'u1', days_ago(40)), ('m5', 'p2', 'u3', days_ago(38)), ('m6', 'p2', 'u4', days_ago(35)),
]

_seed_mind_nodes = [
    ('n1', 'p1', None, '火星基地能源方案', days_ago(12), days_ago(12)),
    ('n2', 'p1', 'n1', '需求分析', days_ago(12), days_ago(11)),
    ('n3', 'p1', 'n1', '能源方案对比', days_ago(12), days_ago(9)),
    ('n4', 'p1', 'n1', '系统集成', days_ago(12), days_ago(8)),
    ('n5', 'p1', 'n2', '基地用电需求估算', days_ago(11), days_ago(11)),
    ('n6', 'p1', 'n2', '昼夜周期与储能', days_ago(11), days_ago(10)),
    ('n7', 'p1', 'n3', '太阳能效率分析', days_ago(10), days_ago(9)),
    ('n8', 'p1', 'n3', '核能小型化方案', days_ago(9), days_ago(9)),
    ('n9', 'p1', 'n4', '能量平衡计算模型', days_ago(8), days_ago(7)),
    ('n10', 'p1', 'n4', '冗余与应急策略', days_ago(8), days_ago(6)),
    ('n11', 'p2', None, '智能垃圾分类助手', days_ago(40), days_ago(30)),
    ('n12', 'p2', 'n11', '分类标准调研', days_ago(39), days_ago(30)),
    ('n13', 'p2', 'n11', '识别模型选型', days_ago(35), days_ago(20)),
    ('n14', 'p2', 'n11', '互动科普模块', days_ago(25), days_ago(12)),
]

_seed_notes = [
    ('sn1', 'p1', '灵感：火星沙尘暴期间太阳能失效，需要备用电源', '#fde68a', 30, 40, days_ago(11), days_ago(11)),
    ('sn2', 'p1', '资料：NASA 好奇号采用 RTG 核电池，寿命超过 14 年', '#bbf7d0', 260, 120, days_ago(10), days_ago(10)),
    ('sn3', 'p1', '讨论结论：主用太阳能 + 备用核能，储能覆盖 12 小时沙尘期', '#bae6fd', 500, 60, days_ago(9), days_ago(8)),
    ('sn4', 'p1', '待查：小型核反应堆的审批与安全标准', '#fbcfe8', 760, 180, days_ago(8), days_ago(8)),
    ('sn5', 'p2', '垃圾分类标准以上海四分类为基础，但食堂场景有特殊要求', '#fde68a', 40, 60, days_ago(30), days_ago(30)),
]

_seed_tasks = [
    ('t1', 'p1', '调研火星基地用电需求', '收集基地照明、生命维持、通信等设备的功率需求，形成需求清单。', 'u1', 'done', days_ago(6), days_ago(10), days_ago(5, 14)),
    ('t2', 'p1', '太阳能板选型与效率计算', '基于火星光照强度与沙尘衰减系数，计算光伏阵列规模。', 'u2', 'done', days_ago(3), days_ago(9), days_ago(2, 11)),
    ('t3', 'p1', '核能方案可行性分析', '调研小型裂变堆与 RTG 的功率密度、寿命与安全性。', 'u3', 'review', days_ago(1), days_ago(8), days_ago(1, 9)),
    ('t4', 'p1', '储能系统设计', '设计电池储能与氢储能组合，覆盖沙尘期供电。', 'u1', 'doing', days_ago(-2), days_ago(6), days_ago(0, 9)),
    ('t5', 'p1', '能量平衡计算模型', '用表格模型对比不同方案的年发电量与可靠性。', None, 'todo', days_ago(-5), days_ago(5), days_ago(5)),
    ('t6', 'p1', '架构图与汇报材料', '绘制能源系统架构图，准备中期汇报。', None, 'todo', days_ago(-7), days_ago(4), days_ago(4)),
    ('t7', 'p2', '四分类标准调研', '整理上海垃圾分类标准与常见误区。', 'u1', 'done', days_ago(30), days_ago(38), days_ago(30, 15)),
    ('t8', 'p2', '图像识别模型测试', '对 200 张校园常见垃圾图片进行识别测试，记录准确率。', 'u3', 'done', days_ago(15), days_ago(30), days_ago(14, 10)),
    ('t9', 'p2', '科普问答模块开发', '开发垃圾分类知识问答与积分激励。', 'u4', 'done', days_ago(9), days_ago(20), days_ago(8, 16)),
    ('t10', 'p2', '原型演示与结题报告', '整合原型，录制演示视频并撰写结题报告。', 'u1', 'done', days_ago(7), days_ago(12), days_ago(6, 15)),
]

_seed_task_logs = [
    ('l1', 'p1', 't1', 'u1', 'create', '创建任务', days_ago(10)),
    ('l2', 'p1', 't1', 'u1', 'claim', '认领任务', days_ago(10, 12)),
    ('l3', 'p1', 't1', 'u1', 'status', '状态更新为 已完成', days_ago(5, 14)),
    ('l4', 'p1', 't3', 'u3', 'claim', '认领任务', days_ago(8, 10)),
    ('l5', 'p1', 't3', 'u3', 'status', '状态更新为 待验收', days_ago(1, 9)),
    ('l6', 'p1', 't4', 'u1', 'claim', '认领任务', days_ago(6, 9)),
    ('l7', 'p1', 't4', 'u1', 'status', '状态更新为 进行中', days_ago(6, 10)),
    ('l8', 'p2', 't7', 'u1', 'create', '创建任务', days_ago(38)),
    ('l9', 'p2', 't10', 'u1', 'status', '状态更新为 已完成', days_ago(6, 15)),
]

_seed_checkins = [
    ('c1', 'p1', 'u1', '完成用电需求调研，清单共 23 项设备', days_ago(5, 15)),
    ('c2', 'p1', 'u2', '光伏阵列计算完成，初步规模 400m²', days_ago(2, 14)),
    ('c3', 'p1', 'u3', '核能方案对比表完成，等待组内评审', days_ago(1, 10)),
    ('c4', 'p1', 'u1', '储能方案初稿完成，开始搭建计算模型', days_ago(0, 9)),
    ('c5', 'p2', 'u1', '调研报告初稿完成', days_ago(30, 16)),
    ('c6', 'p2', 'u3', '识别模型准确率达到 92%', days_ago(14, 11)),
    ('c7', 'p2', 'u4', '科普模块上线测试', days_ago(8, 17)),
    ('c8', 'p2', 'u1', '演示视频录制完成，结题报告提交', days_ago(6, 16)),
]

_seed_feedbacks = [
    ('f1', 'p1', 'u1', 'milestone', FEEDBACK_POOL[0], days_ago(5, 15)),
    ('f2', 'p1', 'u2', 'milestone', FEEDBACK_POOL[2], days_ago(2, 14)),
    ('f3', 'p1', 'u3', 'milestone', FEEDBACK_POOL[3], days_ago(1, 10)),
    ('f4', 'p2', 'u1', 'milestone', FEEDBACK_POOL[1], days_ago(30, 16)),
    ('f5', 'p2', 'u3', 'milestone', FEEDBACK_POOL[4], days_ago(14, 11)),
    ('f6', 'p2', 'u1', 'milestone', FEEDBACK_POOL[5], days_ago(6, 16)),
]

_seed_resources = [
    ('r1', '火星基地能源设计公开课', '物理', '系统讲解火星环境下太阳能与核能的工程设计要点。', 'https://example.com/mars-energy', ['能源', '太空']),
    ('r2', 'PhET 电路搭建实验室', '物理', '在线电路仿真工具，支持太阳能电池与储能电路模拟。', 'https://phet.colorado.edu', ['仿真', '电路']),
    ('r3', 'Khan 学院 · 能量守恒', '物理', '能量守恒与转化率的可视化课程。', 'https://www.khanacademy.org', ['课程', '能量']),
    ('r4', 'NASA 开放数据平台', '工程', '火星探测任务的公开工程数据与设计文档。', 'https://nasa.gov', ['太空', '数据']),
    ('r5', 'Tinkercad 3D 设计', '工程', '浏览器端 3D 建模与电路设计工具，适合原型制作。', 'https://www.tinkercad.com', ['3D', '原型']),
    ('r6', 'Scratch 图形化编程', '编程', '图形化编程入门，适合逻辑训练与交互原型。', 'https://scratch.mit.edu', ['入门', '交互']),
    ('r7', 'Teachable Machine', '编程', '无需代码即可训练图像分类模型，适合垃圾分类识别。', 'https://teachablemachine.withgoogle.com', ['AI', '图像识别']),
    ('r8', 'Codecademy Python 课程', '编程', 'Python 基础与数据处理课程。', 'https://www.codecademy.com', ['Python', '课程']),
    ('r9', '艺术与科学 · 生成艺术', '艺术', '用代码生成视觉艺术的案例集。', 'https://generativeart.com', ['生成艺术']),
    ('r10', '声音可视化案例库', '艺术', '声波可视化的经典交互作品与原理讲解。', 'https://example.com/sound-vis', ['声学', '交互']),
    ('r11', '细胞与生命系统模拟', '生物', '生命维持系统相关的生物循环模拟。', 'https://biomanbio.com', ['生命科学', '模拟']),
    ('r12', '上海市科创资源库', '综合', '虫洞特色科创课程与实验资源总入口。', 'https://example.com/kc-resource', ['虫洞', '综合']),
]

_seed_annotations = [
    ('a1', 'p2', 't1', '识别准确率的测试样本建议扩充到 500 张，覆盖更多食堂场景。', days_ago(20, 9)),
    ('a2', 'p2', 't1', '科普问答的激励机制做得不错，建议补充误分类的纠错引导。', days_ago(12, 14)),
    ('a3', 'p2', 't1', '结题报告结构完整，注意补充能耗对比的量化结论。', days_ago(7, 10)),
]


def seed(db: sqlite3.Connection) -> None:
    """写入演示数据（镜像 InnoArk mock/db.ts 的种子，密码统一为 123456）。"""
    from werkzeug.security import generate_password_hash

    db.executemany(
        'INSERT INTO users (id, username, password, name, role) VALUES (?, ?, ?, ?, ?)',
        [(u[0], u[1], generate_password_hash(u[2]), u[3], u[4]) for u in _seed_users],
    )
    db.executemany(
        'INSERT INTO topics (id, title, summary, subjects, tags, difficulty) VALUES (?, ?, ?, ?, ?, ?)',
        [(t[0], t[1], t[2], json_dumps(t[3]), json_dumps(t[4]), t[5]) for t in _seed_topics],
    )
    db.executemany(
        'INSERT INTO projects (id, topicId, name, status, inviteCode, leaderId, createdAt, updatedAt, finishedAt) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        _seed_projects,
    )
    db.executemany('INSERT INTO members (id, projectId, userId, joinedAt) VALUES (?, ?, ?, ?)', _seed_members)
    db.executemany(
        'INSERT INTO mind_nodes (id, projectId, parentId, label, createdAt, updatedAt) VALUES (?, ?, ?, ?, ?, ?)',
        _seed_mind_nodes,
    )
    db.executemany(
        'INSERT INTO notes (id, projectId, content, color, x, y, createdAt, updatedAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        _seed_notes,
    )
    db.executemany(
        'INSERT INTO tasks (id, projectId, title, description, assigneeId, status, dueDate, createdAt, updatedAt) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        _seed_tasks,
    )
    db.executemany(
        'INSERT INTO task_logs (id, projectId, taskId, userId, action, detail, createdAt) VALUES (?, ?, ?, ?, ?, ?, ?)',
        _seed_task_logs,
    )
    db.executemany(
        'INSERT INTO checkins (id, projectId, userId, content, createdAt) VALUES (?, ?, ?, ?, ?)',
        _seed_checkins,
    )
    db.executemany(
        'INSERT INTO feedbacks (id, projectId, userId, type, content, createdAt) VALUES (?, ?, ?, ?, ?, ?)',
        _seed_feedbacks,
    )
    db.executemany(
        'INSERT INTO resources (id, title, category, description, url, tags) VALUES (?, ?, ?, ?, ?, ?)',
        [(r[0], r[1], r[2], r[3], r[4], json_dumps(r[5])) for r in _seed_resources],
    )
    db.executemany(
        'INSERT INTO annotations (id, projectId, userId, content, createdAt) VALUES (?, ?, ?, ?, ?)',
        _seed_annotations,
    )
    # 专注记录：u1 近 6 天每天 2~4 次番茄钟，u2/u3 各有少量记录
    for d in range(6, 0, -1):
        for _ in range(random.randint(2, 4)):
            db.execute(
                'INSERT INTO focus_sessions (id, userId, durationMin, type, createdAt) VALUES (?, ?, ?, ?, ?)',
                (gen_id('fs'), 'u1', 25, 'focus', days_ago(d, random.randint(9, 20), random.randint(0, 59))),
            )
    for uid in ('u2', 'u3'):
        db.execute(
            'INSERT INTO focus_sessions (id, userId, durationMin, type, createdAt) VALUES (?, ?, ?, ?, ?)',
            (gen_id('fs'), uid, 25, 'focus', days_ago(random.randint(1, 5), random.randint(9, 20), random.randint(0, 59))),
        )
    db.commit()


def init_db() -> None:
    """建表（含轻量迁移）；users 为空时写入种子数据。"""
    db = get_db()
    db.executescript(SCHEMA)
    cols = [r['name'] for r in db.execute('PRAGMA table_info(projects)').fetchall()]
    if 'description' not in cols:
        db.execute("ALTER TABLE projects ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        db.commit()
    if not query_one('SELECT 1 FROM users LIMIT 1'):
        seed(db)


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def json_loads(value) -> list:
    return json.loads(value) if value else []
