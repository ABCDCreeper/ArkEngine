"""跨学科资源导航路由（契约 3.6）：分类浏览 + 关键词搜索。"""
from flask import Blueprint, jsonify, request

from ..auth import require_auth
from ..db import query_all
from ..services import paged, resource_view

bp = Blueprint('resources', __name__)


@bp.get('/resources')
@require_auth
def list_resources():
    """?category=物理 / ?keyword=AI（匹配标题、描述、标签，大小写不敏感）。"""
    items = [resource_view(r) for r in query_all('SELECT * FROM resources')]
    category = request.args.get('category')
    keyword = request.args.get('keyword')
    if category:
        items = [r for r in items if r['category'] == category]
    if keyword:
        kw = keyword.lower()
        items = [r for r in items
                 if kw in r['title'].lower() or kw in r['description'].lower()
                 or any(kw in t.lower() for t in r['tags'])]
    return jsonify(paged(items))
