import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Config:
    """应用配置（可用 create_app(config) 覆盖，测试时传入临时 DATABASE）。"""

    # SQLite 数据库文件路径
    DATABASE = os.environ.get('ARK_DATABASE') or os.path.join(BASE_DIR, 'instance', 'innoark.db')

    # 前端构建产物目录（存在时由后端一并提供页面；可用 ARK_FRONTEND_DIST 覆盖）
    FRONTEND_DIST = os.path.normpath(
        os.environ.get('ARK_FRONTEND_DIST') or os.path.join(BASE_DIR, '..', 'InnoArk-main', 'dist')
    )

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

    # 口令哈希方法：留空用 werkzeug 默认（scrypt）。测试注入廉价算法以缩短套件耗时。
    PASSWORD_HASH_METHOD = os.environ.get('ARK_PASSWORD_HASH') or None

    # 会话有效期（天）；过期 token 由 parse_token 拒绝
    SESSION_TTL_DAYS = 14
