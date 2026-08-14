"""蓝图注册：所有子模块挂到 /api 前缀下。"""
from flask import Blueprint

from . import activity, auth, focus, kanban, projects, quiz, resources, tasks, teacher

api = Blueprint('api', __name__)

for module in (auth, projects, kanban, tasks, activity, resources, focus, teacher, quiz):
    api.register_blueprint(module.bp)


def register_blueprints(app) -> None:
    app.register_blueprint(api, url_prefix='/api')
