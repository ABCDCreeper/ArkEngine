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
  groupId TEXT,
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
CREATE TABLE IF NOT EXISTS quiz_questions (
  id TEXT PRIMARY KEY,
  groupId TEXT,
  createdBy TEXT,
  createdAt TEXT,
  updatedAt TEXT,
  category TEXT NOT NULL,
  difficulty INTEGER NOT NULL DEFAULT 1,
  question TEXT NOT NULL,
  options TEXT NOT NULL DEFAULT '[]',
  answer INTEGER NOT NULL,
  explanation TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS quiz_attempts (
  id TEXT PRIMARY KEY,
  userId TEXT NOT NULL,
  score INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  createdAt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  quizMode TEXT NOT NULL DEFAULT 'group' CHECK (quizMode IN ('group', 'fallback', 'mixed')),
  inviteCode TEXT NOT NULL DEFAULT '',
  createdAt TEXT NOT NULL,
  updatedAt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
  id TEXT PRIMARY KEY,
  groupId TEXT NOT NULL,
  userId TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('teacher', 'member')),
  joinedAt TEXT NOT NULL,
  UNIQUE (groupId, userId)
);
CREATE TABLE IF NOT EXISTS group_invites (
  id TEXT PRIMARY KEY,
  groupId TEXT NOT NULL,
  userId TEXT NOT NULL,
  inviterId TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'declined')),
  createdAt TEXT NOT NULL,
  respondedAt TEXT
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


def gen_group_invite_code() -> str:
    """生成全局唯一的用户组邀请码（如 G-KM3X 风格）。"""
    while True:
        code = 'G' + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
        if not query_one('SELECT 1 FROM groups WHERE inviteCode = ?', (code,)):
            return code


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
    ('p1', 'topic1', 'g1', '火星基地能源方案', 'active', 'P1-7F3A', 'u1', days_ago(12), days_ago(0, 9), None),
    ('p2', 'topic2', 'g1', '校园智能垃圾分类助手', 'finished', 'P2-9B1C', 'u1', days_ago(40), days_ago(6, 16), days_ago(6, 17)),
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

_seed_quiz_questions = [
    # (id, category, difficulty, question, options, answer, explanation)
    ('q1', '物理', 1, '火星沙尘暴期间，到达地面的阳光最多会减少约多少？',
     ['5% 左右', '20% 左右', '60% 左右', '90% 以上'], 2,
     '火星全球性沙尘暴可遮挡约 60% 的阳光，这正是火星基地必须搭配备用能源的原因。'),
    ('q2', '物理', 1, 'NASA 好奇号火星车靠什么供电？',
     ['太阳能电池板', '小型核反应堆', '放射性同位素热电发电机（RTG）', '氢燃料电池'], 2,
     'RTG 把钚-238 衰变产生的热量直接转化为电，不依赖阳光，寿命超过 14 年。'),
    ('q3', '物理', 1, '声音在以下哪种介质中传播最快？',
     ['空气', '水', '钢铁', '真空'], 2,
     '声音靠粒子振动传播，粒子排列越紧密传得越快：钢铁 > 水 > 空气，真空里根本传不了。'),
    ('q4', '物理', 2, '能量守恒定律的本质是？',
     ['能量可以被创造出来', '能量只会转化，不会凭空消失', '能量只会越来越少', '摩擦会让能量消失'], 1,
     '能量既不会凭空产生也不会消失，只会从一种形式转化为另一种形式——摩擦只是把机械能变成了内能。'),
    ('q5', '工程', 1, '商用太阳能电池板的发电效率一般在什么范围？',
     ['1% ~ 3%', '18% ~ 23%', '50% ~ 60%', '接近 100%'], 1,
     '单晶硅光伏板效率一般在 18%~23%，NASA 实验室的纪录也远没到 50%，所以面积规划很关键。'),
    ('q6', '工程', 2, '锂电池在火星环境面临的最大挑战是？',
     ['太重', '低温下容量和功率大幅下降', '容易起火', '价格太贵'], 1,
     '火星夜间可低至 -100℃ 以下，锂电池低温性能严重衰减，必须加保温设计——储能不是简单堆电池。'),
    ('q7', '工程', 2, '3D 打印的承重结构为什么能撑住重物？',
     ['打印材料密度更高', '结构力学设计（蜂窝、桁架等）', '打印速度更快', '用了强力胶水'], 1,
     '承重能力来自拓扑优化与仿生结构设计，用最少的材料获得最大的强度，而不是材料本身。'),
    ('q8', '工程', 1, '电路中的“短路”是指？',
     ['电流绕过用电器直接流通', '电池没电了', '导线太细', '开关断开'], 0,
     '短路时电流几乎不经过负载，回路电阻极小、电流极大，很容易烧毁导线和电源。'),
    ('q9', '编程', 1, '8 个二进制位（bit）等于？',
     ['1 字节（byte）', '1 千字节', '1 兆字节', '1 个汉字'], 0,
     '8 bit = 1 byte，一个英文字符通常占 1 字节，一个汉字约占 3 字节（UTF-8）。'),
    ('q10', '编程', 1, '以下哪个是程序里的“循环”结构？',
     ['如果下雨就带伞', '每天做 100 个俯卧撑直到达标', '先吃饭再去上课', '从 A、B 中选一个'], 1,
     '循环就是重复执行某段代码直到条件不满足；“如果”是条件判断，“先…再…”是顺序执行。'),
    ('q11', '编程', 2, '给垃圾分类识别模型“训练”，本质上是？',
     ['给模型看大量带标签的例子，让它自己总结规律', '把图片压缩变小', '手工写一堆判断规则', '让模型把答案背下来'], 0,
     '机器学习训练 = 从海量标注数据中自动总结特征规律，Teachable Machine 就是这么工作的。'),
    ('q12', '编程', 1, '程序里的“变量”是干什么用的？',
     ['让代码看起来更长', '存储并复用数据', '变量越多程序越快', '纯粹为了美观'], 1,
     '变量就是给数据起的名字，可以随时存取和修改，是程序组织数据的基本单位。'),
    ('q13', '生物', 1, '光合作用主要发生在植物细胞的哪个结构？',
     ['线粒体', '叶绿体', '细胞核', '液泡'], 1,
     '叶绿体是“光合工厂”，把光能变成化学能；线粒体才是细胞里烧能量（呼吸作用）的发电站。'),
    ('q14', '生物', 1, '红细胞的主要工作是？',
     ['抵抗病毒', '运输氧气', '让血液凝固', '消化食物'], 1,
     '红细胞中的血红蛋白结合氧气，把氧从肺送到全身；免疫是白细胞和抗体的活儿。'),
    ('q15', '生物', 2, '人的呼吸会把吸入的氧气变成什么？',
     ['氮气', '二氧化碳', '氢气', '臭氧'], 1,
     '细胞呼吸消耗氧气、产生二氧化碳；太空舱得把 CO₂ 再变回氧气（比如电解水）才能循环。'),
    ('q16', '生物', 1, '生态系统中扮演“清洁工”角色的是？',
     ['病毒', '分解者（细菌、真菌）', '蓝藻', '寄生虫'], 1,
     '分解者把动植物遗体分解成无机物归还环境，让物质循环起来——没有它们地球早就堆满了。'),
    ('q17', '综合', 1, '洗净压扁的塑料饮料瓶应该扔进哪个垃圾桶？',
     ['可回收', '有害', '湿垃圾', '干垃圾'], 0,
     '干净塑料瓶属于可回收物；但沾满油污的塑料瓶回收价值大减，可能只能算干垃圾。'),
    ('q18', '综合', 1, '经典番茄工作法中，一个“番茄钟”的专注时长是？',
     ['15 分钟', '25 分钟', '45 分钟', '60 分钟'], 1,
     '25 分钟专注 + 5 分钟休息，正是 InnoArk 专注页的设定——短周期更容易进入心流。'),
    ('q19', '综合', 2, '二维码比一维条形码信息量大得多，主要因为？',
     ['它更大', '横竖两个方向都能编码', '颜色更多', '图案更复杂'], 1,
     '二维码是矩阵码，在水平和垂直两个方向同时编码，信息密度是一维条码的几十倍。'),
    ('q20', '综合', 1, '爱迪生试了几千种灯丝材料才成功，这说明？',
     ['失败说明这条路不行', '每次失败都是数据，帮你排除错误假设', '应该放弃尝试', '失败越多越接近运气'], 1,
     '科学探索中“失败”同样有价值：每排除一种假设，就离正确答案近一步——这就是科学方法。'),
]

_seed_groups = [
    ('g1', '火星能源课题小组', '火星基地能源课题的同学们，由指导老师负责出题。', 'fallback', 'G1-KM3X', days_ago(10), days_ago(10)),
]

_seed_group_members = [
    ('gm1', 'g1', 't1', 'teacher', days_ago(10)),
    ('gm2', 'g1', 'u1', 'member', days_ago(9)),
    ('gm3', 'g1', 'u2', 'member', days_ago(8)),
    ('gm4', 'g1', 'u3', 'member', days_ago(7)),
]

_seed_group_questions = [
    # (id, groupId, createdBy, createdAt, updatedAt, category, difficulty, question, options, answer, explanation)
    ('q21', 'g1', 't1', days_ago(9), days_ago(9), '物理', 1, '火星上的一个太阳日比地球的一天大约？',
     ['差不多一样长', '长约 40 分钟', '短约 40 分钟', '长约 3 小时'], 1,
     '火星自转周期约 24 小时 39 分，比地球多约 40 分钟——作息表得按火星时间重排。'),
    ('q22', 'g1', 't1', days_ago(9), days_ago(9), '物理', 1, '火星大气的主要成分是？',
     ['氧气', '氮气', '二氧化碳（约 95%）', '氦气'], 2,
     '火星大气 95% 以上是二氧化碳，氧气含量不到 0.2%，所以基地必须自己产氧。'),
    ('q23', 'g1', 't1', days_ago(8), days_ago(8), '物理', 2, '火星表面的重力约为地球的？',
     ['约 1/8', '约 1/3', '约 1/2', '与地球相同'], 1,
     '火星质量约为地球的 1/10，表面重力约 0.38g——跳起来能比地球高两倍多。'),
    ('q24', 'g1', 't1', days_ago(8), days_ago(8), '工程', 2, '地球与火星的最近距离大约是多少？',
     ['5500 万公里', '2 亿公里', '5 亿公里', '1 光年'], 0,
     '两者最近约 5500 万公里（大概每 26 个月一次窗口期），信号单程就要 3 分钟以上。'),
    ('q25', 'g1', 't1', days_ago(7), days_ago(7), '工程', 2, '火星着陆器进入大气层后，通常靠什么减速？',
     ['降落伞 + 反推火箭', '直接撞击缓冲', '系绳吊放', '磁悬浮刹车'], 0,
     '先靠大气摩擦减速，再开降落伞，最后反推火箭软着陆——好奇号用的就是这套组合。'),
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
        'INSERT INTO projects (id, topicId, groupId, name, status, inviteCode, leaderId, createdAt, updatedAt, finishedAt) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
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
    db.executemany(
        'INSERT INTO groups (id, name, description, quizMode, inviteCode, createdAt, updatedAt) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        _seed_groups,
    )
    db.executemany(
        'INSERT INTO group_members (id, groupId, userId, role, joinedAt) VALUES (?, ?, ?, ?, ?)',
        _seed_group_members,
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


def seed_quiz_public(db: sqlite3.Connection) -> None:
    """写入公共题库（无分组的用户与回退场景使用）。"""
    db.executemany(
        'INSERT INTO quiz_questions (id, category, difficulty, question, options, answer, explanation) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        [(q[0], q[1], q[2], q[3], json_dumps(q[4]), q[5], q[6]) for q in _seed_quiz_questions],
    )
    db.commit()


def seed_quiz_group(db: sqlite3.Connection) -> None:
    """写入演示组 g1 的组内题库（仅当 g1 存在时）。"""
    if not query_one('SELECT 1 FROM groups WHERE id = ?', ('g1',)):
        return
    db.executemany(
        'INSERT INTO quiz_questions (id, groupId, createdBy, createdAt, updatedAt, category, difficulty, '
        'question, options, answer, explanation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [(q[0], q[1], q[2], q[3], q[4], q[5], q[6], q[7], json_dumps(q[8]), q[9], q[10]) for q in _seed_group_questions],
    )
    db.commit()


def seed_groups(db: sqlite3.Connection) -> None:
    """写入演示用户组与成员（独立于主种子，保证既有数据库也能补齐）。"""
    db.executemany(
        'INSERT INTO groups (id, name, description, quizMode, inviteCode, createdAt, updatedAt) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        _seed_groups,
    )
    db.executemany(
        'INSERT INTO group_members (id, groupId, userId, role, joinedAt) VALUES (?, ?, ?, ?, ?)',
        _seed_group_members,
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
    qcols = [r['name'] for r in db.execute('PRAGMA table_info(quiz_questions)').fetchall()]
    for col, ddl in (
        ('groupId', 'TEXT'),
        ('createdBy', 'TEXT'),
        ('createdAt', 'TEXT'),
        ('updatedAt', 'TEXT'),
    ):
        if col not in qcols:
            db.execute(f'ALTER TABLE quiz_questions ADD COLUMN {col} {ddl}')
            db.commit()
    gcols = [r['name'] for r in db.execute('PRAGMA table_info(groups)').fetchall()]
    if 'inviteCode' not in gcols:
        db.execute("ALTER TABLE groups ADD COLUMN inviteCode TEXT NOT NULL DEFAULT ''")
        db.commit()
    pcols = [r['name'] for r in db.execute('PRAGMA table_info(projects)').fetchall()]
    if 'groupId' not in pcols:
        db.execute('ALTER TABLE projects ADD COLUMN groupId TEXT')
        db.commit()
    for g in query_all("SELECT id FROM groups WHERE inviteCode = ''"):
        execute('UPDATE groups SET inviteCode = ? WHERE id = ?', (gen_group_invite_code(), g['id']))
        db.commit()
    if not query_one('SELECT 1 FROM users LIMIT 1'):
        seed(db)
    if not query_one('SELECT 1 FROM groups LIMIT 1'):
        seed_groups(db)
    if not query_one('SELECT 1 FROM quiz_questions WHERE groupId IS NULL LIMIT 1'):
        seed_quiz_public(db)
    if not query_one("SELECT 1 FROM quiz_questions WHERE groupId = 'g1' LIMIT 1"):
        seed_quiz_group(db)


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def json_loads(value) -> list:
    return json.loads(value) if value else []
