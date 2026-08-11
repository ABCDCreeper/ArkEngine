"""InnoArk「虫洞·星桥」后端 — Flask 应用工厂。

按 InnoArk 前端 API 契约（docs/api.md）实现；依赖仅 Flask + SQLite。
"""
import os

from flask import Flask

from . import db
from .config import Config
from .errors import register_error_handlers
from .routes import register_blueprints


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    os.makedirs(os.path.dirname(app.config['DATABASE']) or '.', exist_ok=True)

    # 中文内容原样输出（不转义为 \uXXXX）
    app.json.ensure_ascii = False
    app.json.mimetype = 'application/json; charset=utf-8'

    db.init_app(app)
    register_error_handlers(app)
    register_blueprints(app)

    with app.app_context():
        db.init_db()

    return app
