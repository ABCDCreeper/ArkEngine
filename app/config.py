import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Config:
    """应用配置（可用 create_app(config) 覆盖，测试时传入临时 DATABASE）。"""

    # SQLite 数据库文件路径
    DATABASE = os.path.join(BASE_DIR, 'instance', 'innoark.db')

    # 演示账号（种子数据）
    DEMO_ACCOUNTS = {
        'student': '123456',
        'student2': '123456',
        'student3': '123456',
        'student4': '123456',
        'teacher': '123456',
    }

    # 队伍人数上限（契约 1.6）
    TEAM_LIMIT = 4
