"""星云创意看板路由（契约 3.3）：思维导图节点 + 灵感便签。"""
from flask import Blueprint, g, jsonify

from ..auth import require_auth
from ..db import commit, execute, gen_id, now_iso, query_all, query_one
from ..errors import bad_request, get_json_body, not_found
from ..services import ensure_read_access, get_project_or_404, member_of, paged, touch_project

bp = Blueprint('kanban', __name__)


# ---------------------------------------------------------------- 思维导图

@bp.get('/projects/<project_id>/mind-nodes')
@require_auth
def list_mind_nodes(project_id):
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    items = query_all('SELECT * FROM mind_nodes WHERE projectId = ?', (project['id'],))
    return jsonify(paged(items))


@bp.post('/projects/<project_id>/mind-nodes')
@require_auth
def create_mind_node(project_id):
    project = get_project_or_404(project_id)
    member_of(project['id'], g.user)
    body = get_json_body()
    label = str(body.get('label') or '').strip()
    if not label:
        raise bad_request('节点内容不能为空')
    parent_id = body.get('parentId')
    if parent_id and not query_one('SELECT 1 FROM mind_nodes WHERE id = ? AND projectId = ?',
                                   (parent_id, project['id'])):
        raise bad_request('父节点不存在')
    now = now_iso()
    node = {'id': gen_id('n'), 'projectId': project['id'], 'parentId': parent_id,
            'label': label, 'createdAt': now, 'updatedAt': now}
    execute('INSERT INTO mind_nodes (id, projectId, parentId, label, createdAt, updatedAt) VALUES (?, ?, ?, ?, ?, ?)',
            tuple(node.values()))
    touch_project(project['id'])
    commit()
    return jsonify(node), 201


@bp.patch('/mind-nodes/<node_id>')
@require_auth
def rename_mind_node(node_id):
    node = query_one('SELECT * FROM mind_nodes WHERE id = ?', (node_id,))
    if not node:
        raise not_found('节点不存在')
    project = get_project_or_404(node['projectId'])
    member_of(project['id'], g.user)
    body = get_json_body()
    if 'label' in body:
        label = str(body['label'] or '').strip()
        if not label:
            raise bad_request('节点内容不能为空')
        execute('UPDATE mind_nodes SET label = ? WHERE id = ?', (label, node['id']))
    execute('UPDATE mind_nodes SET updatedAt = ? WHERE id = ?', (now_iso(), node['id']))
    touch_project(project['id'])
    commit()
    return jsonify(query_one('SELECT * FROM mind_nodes WHERE id = ?', (node_id,)))


def collect_descendants(node_id: str) -> list[str]:
    """递归收集节点及其全部子节点（删除含子树，契约 3.3）。"""
    ids = [node_id]
    for row in query_all('SELECT id FROM mind_nodes WHERE parentId = ?', (node_id,)):
        ids.extend(collect_descendants(row['id']))
    return ids


@bp.delete('/mind-nodes/<node_id>')
@require_auth
def delete_mind_node(node_id):
    node = query_one('SELECT * FROM mind_nodes WHERE id = ?', (node_id,))
    if not node:
        raise not_found('节点不存在')
    project = get_project_or_404(node['projectId'])
    member_of(project['id'], g.user)
    ids = collect_descendants(node_id)
    execute(f'DELETE FROM mind_nodes WHERE id IN ({",".join("?" * len(ids))})', ids)
    touch_project(project['id'])
    commit()
    return '', 204


# ---------------------------------------------------------------- 灵感便签

@bp.get('/projects/<project_id>/notes')
@require_auth
def list_notes(project_id):
    project = get_project_or_404(project_id)
    ensure_read_access(project['id'], g.user)
    items = query_all('SELECT * FROM notes WHERE projectId = ?', (project['id'],))
    return jsonify(paged(items))


@bp.post('/projects/<project_id>/notes')
@require_auth
def create_note(project_id):
    project = get_project_or_404(project_id)
    member_of(project['id'], g.user)
    body = get_json_body()
    now = now_iso()
    note = {
        'id': gen_id('sn'),
        'projectId': project['id'],
        'content': str(body.get('content') or ''),
        'color': body.get('color') or '#fde68a',
        'x': body.get('x') if isinstance(body.get('x'), (int, float)) else 20,
        'y': body.get('y') if isinstance(body.get('y'), (int, float)) else 20,
        'createdAt': now,
        'updatedAt': now,
    }
    execute('INSERT INTO notes (id, projectId, content, color, x, y, createdAt, updatedAt) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            tuple(note.values()))
    touch_project(project['id'])
    commit()
    return jsonify(note), 201


@bp.patch('/notes/<note_id>')
@require_auth
def update_note(note_id):
    note = query_one('SELECT * FROM notes WHERE id = ?', (note_id,))
    if not note:
        raise not_found('便签不存在')
    project = get_project_or_404(note['projectId'])
    member_of(project['id'], g.user)
    body = get_json_body()
    fields = []
    args = []
    if 'content' in body:
        fields.append('content = ?')
        args.append(str(body['content'] or ''))
    if 'color' in body:
        fields.append('color = ?')
        args.append(body['color'])
    if isinstance(body.get('x'), (int, float)):
        fields.append('x = ?')
        args.append(body['x'])
    if isinstance(body.get('y'), (int, float)):
        fields.append('y = ?')
        args.append(body['y'])
    fields.append('updatedAt = ?')
    args.append(now_iso())
    args.append(note_id)
    execute(f'UPDATE notes SET {", ".join(fields)} WHERE id = ?', args)
    touch_project(project['id'])
    commit()
    return jsonify(query_one('SELECT * FROM notes WHERE id = ?', (note_id,)))


@bp.delete('/notes/<note_id>')
@require_auth
def delete_note(note_id):
    note = query_one('SELECT * FROM notes WHERE id = ?', (note_id,))
    if not note:
        raise not_found('便签不存在')
    project = get_project_or_404(note['projectId'])
    member_of(project['id'], g.user)
    execute('DELETE FROM notes WHERE id = ?', (note_id,))
    touch_project(project['id'])
    commit()
    return '', 204
