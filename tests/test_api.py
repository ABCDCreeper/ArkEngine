"""InnoArk 后端端到端测试（Flask test client + 临时 SQLite）。

运行：python -m unittest discover -s tests -v
每个用例使用独立临时数据库（种子数据保持一致），互不影响。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.services import FEEDBACK_POOL  # noqa: E402
from app.db import query_all, query_one, execute, commit, iso  # noqa: E402


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        # 测试期换用廉价口令哈希：每个用例都要播种账号并登录 7 次，
        # 生产默认的 scrypt 单次约 50ms，会让整个套件多花一分多钟。
        self.app = create_app({'DATABASE': self.db_path, 'PASSWORD_HASH_METHOD': 'pbkdf2:sha256:1000'})
        self.client = self.app.test_client()
        self.student = self._login('student', '123456')
        self.student2 = self._login('student2', '123456')
        self.student4 = self._login('student4', '123456')
        self.teacher = self._login('teacher', '123456')
        self.superadmin = self._login('superadmin', '123456')
        self.admin = self._login('admin', '123456')
        self.schooladmin = self._login('schooladmin', '123456')

    def tearDown(self):
        os.unlink(self.db_path)

    # ------------------------------------------------------------ 工具方法

    def _login(self, username, password):
        res = self.client.post('/api/sessions', json={'username': username, 'password': password})
        assert res.status_code == 201, (username, res.status_code, res.get_json())
        data = res.get_json()
        return {'token': data['token'], 'user': data['user']}

    def _request(self, user, method, path, json=None):
        kw = {'headers': {'Authorization': f"Bearer {user['token']}"}}
        if json is not None:
            kw['json'] = json
        return getattr(self.client, method)(path, **kw)

    def _get(self, user, path):
        return self._request(user, 'get', path)

    def _post(self, user, path, body=None):
        return self._request(user, 'post', path, body)

    def _patch(self, user, path, body=None):
        return self._request(user, 'patch', path, body)

    def _delete(self, user, path):
        return self._request(user, 'delete', path)

    def _put(self, user, path, body=None):
        return self._request(user, 'put', path, body)

    def assert_error(self, res, status, code):
        self.assertEqual(res.status_code, status)
        self.assertEqual(res.get_json()['error']['code'], code)

    def create_project(self, user, topic_id='topic1', name=None):
        res = self._post(user, '/api/projects', {'topicId': topic_id, 'name': name} if name else {'topicId': topic_id})
        self.assertEqual(res.status_code, 201, res.get_json())
        return res.get_json()

    def create_task(self, user, project_id, title='测试任务'):
        res = self._post(user, f'/api/projects/{project_id}/tasks', {'title': title})
        self.assertEqual(res.status_code, 201, res.get_json())
        return res.get_json()

    def register_teacher(self, username='tea2'):
        res = self._post(self.schooladmin, '/api/admin/users', {
            'username': username, 'password': '123456', 'name': '李老师', 'role': 'teacher',
        })
        self.assertEqual(res.status_code, 201, res.get_json())
        return self._login(username, '123456')

    def complete_task(self, task_id, user=None):
        user = user or self.student
        path = f'/api/tasks/{task_id}'
        self.assertEqual(self._patch(user, path, {'assigneeId': user['user']['id']}).status_code, 200)
        for status in ('doing', 'review'):
            self.assertEqual(self._patch(user, path, {'status': status}).status_code, 200)
        self.assertEqual(self._patch(self.teacher, path, {'status': 'done'}).status_code, 200)

    def play_round(self, user, correct, count=10):
        round_data = self._get(user, f'/api/quiz/questions?count={count}').get_json()
        for index, q in enumerate(round_data['items']):
            with self.app.app_context():
                answer = query_one('SELECT answer FROM quiz_questions WHERE id = ?', (q['id'],))['answer']
            choice = answer if index < correct else (answer + 1) % len(q['options'])
            res = self._post(user, f"/api/quiz/rounds/{round_data['roundId']}/answers",
                             {'questionId': q['id'], 'choice': choice})
            self.assertEqual(res.status_code, 200, res.get_json())
        return self._post(user, '/api/quiz/attempts', {'roundId': round_data['roundId']})

    # ------------------------------------------------------------ 认证

    def test_register_success(self):
        """注册成功即登录态：返回 token + user，且可用新账号登录访问。"""
        res = self.client.post('/api/users', json={
            'username': 'alice', 'password': '123456', 'name': '爱丽丝', 'role': 'student',
        })
        self.assertEqual(res.status_code, 201)
        body = res.get_json()
        self.assertEqual(body['user']['username'], 'alice')
        self.assertEqual(body['user']['name'], '爱丽丝')
        self.assertEqual(body['user']['role'], 'student')
        self.assertNotIn('password', body['user'])
        self.assertTrue(body['token'])
        # 注册的 token 直接可用
        res = self.client.get('/api/me', headers={'Authorization': f"Bearer {body['token']}"})
        self.assertEqual(res.get_json()['user']['id'], body['user']['id'])
        # 新账号可正常登录
        res = self.client.post('/api/sessions', json={'username': 'alice', 'password': '123456'})
        self.assertEqual(res.status_code, 201)
        # 新用户无任何项目
        token = res.get_json()['token']
        res = self.client.get('/api/projects', headers={'Authorization': f"Bearer {token}"})
        self.assertEqual(res.get_json()['items'], [])

    def test_register_teacher_role(self):
        res = self.client.post('/api/users', json={
            'username': 'teacher2', 'password': '123456', 'name': '李老师', 'role': 'teacher',
        })
        self.assert_error(res, 400, 'VALIDATION_ERROR')
        teacher = self.register_teacher('teacher2')
        self.assertEqual(teacher['user']['role'], 'teacher')

    def test_register_username_taken(self):
        res = self.client.post('/api/users', json={
            'username': 'student', 'password': '123456', 'name': '重复', 'role': 'student',
        })
        self.assert_error(res, 409, 'USERNAME_TAKEN')

    def test_register_validation(self):
        base = {'username': 'alice', 'password': '123456', 'name': '爱丽丝', 'role': 'student'}
        # 缺失字段
        for missing in ('username', 'password', 'name'):
            body = {k: v for k, v in base.items() if k != missing}
            self.assert_error(self.client.post('/api/users', json=body), 400, 'VALIDATION_ERROR')
        # 用户名过短
        body = {**base, 'username': 'ab'}
        self.assert_error(self.client.post('/api/users', json=body), 400, 'VALIDATION_ERROR')
        # 密码过短
        body = {**base, 'password': '12345'}
        self.assert_error(self.client.post('/api/users', json=body), 400, 'VALIDATION_ERROR')
        # 非法角色
        body = {**base, 'role': 'admin'}
        self.assert_error(self.client.post('/api/users', json=body), 400, 'VALIDATION_ERROR')
        # 失败不产生登录态
        res = self.client.post('/api/sessions', json={'username': 'alice', 'password': '123456'})
        self.assert_error(res, 401, 'INVALID_CREDENTIALS')

    def test_login_success(self):
        self.assertEqual(self.student['user']['id'], 'u1')
        self.assertEqual(self.student['user']['role'], 'student')
        self.assertNotIn('password', self.student['user'])

    def test_login_bad_credentials(self):
        res = self._post(self.student, '/api/sessions', {'username': 'student', 'password': 'wrong'})
        self.assert_error(res, 401, 'INVALID_CREDENTIALS')

    def test_login_missing_fields(self):
        res = self._post(self.student, '/api/sessions', {'username': 'student'})
        self.assert_error(res, 400, 'VALIDATION_ERROR')

    def test_unauthorized(self):
        res = self.client.get('/api/topics')
        self.assert_error(res, 401, 'UNAUTHORIZED')

    def test_invalid_token(self):
        res = self.client.get('/api/topics', headers={'Authorization': 'Bearer bad.token'})
        self.assert_error(res, 401, 'UNAUTHORIZED')

    def test_me(self):
        res = self._get(self.teacher, '/api/me')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['user']['role'], 'teacher')

    def test_logout_invalidates_token(self):
        res = self._delete(self.student, '/api/sessions/current')
        self.assertEqual(res.status_code, 204)
        res = self._get(self.student, '/api/me')
        self.assert_error(res, 401, 'UNAUTHORIZED')

    # ------------------------------------------------------------ 课题与项目

    def test_topics(self):
        res = self._get(self.student, '/api/topics')
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body['total'], 4)
        topic = body['items'][0]
        self.assertIsInstance(topic['subjects'], list)
        self.assertIn('difficulty', topic)

    def test_my_projects(self):
        res = self._get(self.student, '/api/projects')
        ids = [p['id'] for p in res.get_json()['items']]
        self.assertIn('p1', ids)
        self.assertIn('p2', ids)
        # 教师无参与项目，返回空数组
        res = self._get(self.teacher, '/api/projects')
        self.assertEqual(res.get_json()['items'], [])

    def test_create_project(self):
        project = self.create_project(self.student2, 'topic2', '新项目')
        self.assertEqual(project['leaderId'], 'u2')
        self.assertEqual(project['status'], 'active')
        self.assertEqual(project['name'], '新项目')
        self.assertTrue(project['inviteCode'].startswith('P'))
        self.assertEqual([m['id'] for m in project['members']], ['u2'])
        # 根导图节点已初始化
        res = self._get(self.student2, f"/api/projects/{project['id']}/mind-nodes")
        nodes = res.get_json()['items']
        self.assertEqual(len(nodes), 1)
        self.assertIsNone(nodes[0]['parentId'])
        # name 省略时默认取课题名
        project2 = self.create_project(self.student2, 'topic3')
        self.assertEqual(project2['name'], '星舰生命维持系统')

    def test_join_project(self):
        # 跨组隔离：u4（未分组）不能通过邀请码加入 g1 的 p1
        res = self._post(self.student4, '/api/projects/join', {'inviteCode': 'P1-7F3A'})
        self.assert_error(res, 403, 'FORBIDDEN')
        # 已是成员的组员再加入 -> ALREADY_MEMBER（大小写不敏感）
        res = self._post(self.student2, '/api/projects/join', {'inviteCode': 'p1-7f3a'})
        self.assert_error(res, 409, 'ALREADY_MEMBER')
        # 无效邀请码
        res = self._post(self.student4, '/api/projects/join', {'inviteCode': 'P9-XXXX'})
        self.assert_error(res, 409, 'INVALID_INVITE')
        # 公共项目任意学生可凭码加入
        p = self.create_project(self.student4, 'topic2', '公共项目')
        res = self._post(self.student2, '/api/projects/join', {'inviteCode': p['inviteCode']})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(len(res.get_json()['members']), 2)
        # 教师不能加入项目
        res = self._post(self.teacher, '/api/projects/join', {'inviteCode': p['inviteCode']})
        self.assert_error(res, 403, 'FORBIDDEN')

    def test_project_detail_permissions(self):
        # 非成员学生 -> 403
        res = self._get(self.student4, '/api/projects/p1')
        self.assert_error(res, 403, 'FORBIDDEN')
        # 教师可读任意项目
        res = self._get(self.teacher, '/api/projects/p1')
        self.assertEqual(res.status_code, 200)
        # 不存在的项目 -> 404 PROJECT_NOT_FOUND
        res = self._get(self.student, '/api/projects/nope')
        self.assert_error(res, 404, 'PROJECT_NOT_FOUND')

    def test_update_project(self):
        res = self._patch(self.student, '/api/projects/p1', {'name': '火星基地能源方案 v2'})
        self.assertEqual(res.get_json()['name'], '火星基地能源方案 v2')
        # 空名称 -> 400
        res = self._patch(self.student, '/api/projects/p1', {'name': ''})
        self.assert_error(res, 400, 'VALIDATION_ERROR')
        # 非成员改名 -> 403
        res = self._patch(self.student4, '/api/projects/p1', {'name': 'x'})
        self.assert_error(res, 403, 'FORBIDDEN')

    def test_update_project_description(self):
        # 组员填写简介
        res = self._patch(self.student, '/api/projects/p1', {'description': '探索火星基地的能源自给方案'})
        body = res.get_json()
        self.assertEqual(body['description'], '探索火星基地的能源自给方案')
        # 教师也可填写简介
        res = self._patch(self.teacher, '/api/projects/p1', {'description': '教师修订的简介'})
        self.assertEqual(res.get_json()['description'], '教师修订的简介')
        # 简介可清空
        res = self._patch(self.student, '/api/projects/p1', {'description': ''})
        self.assertEqual(res.get_json()['description'], '')
        # 非字符串 -> 400
        res = self._patch(self.student, '/api/projects/p1', {'description': 123})
        self.assert_error(res, 400, 'VALIDATION_ERROR')
        # 超长 -> 400
        res = self._patch(self.student, '/api/projects/p1', {'description': 'x' * 2001})
        self.assert_error(res, 400, 'VALIDATION_ERROR')
        # 非成员不可修改
        res = self._patch(self.student4, '/api/projects/p1', {'description': 'x'})
        self.assert_error(res, 403, 'FORBIDDEN')
        # GET 返回简介
        res = self._get(self.student, '/api/projects/p1')
        self.assertIn('description', res.get_json())

    def test_finish_project(self):
        project = self.create_project(self.student)
        pid = project['id']
        task = self.create_task(self.student, pid)
        self.complete_task(task['id'])
        res = self._patch(self.student, f'/api/projects/{pid}', {'status': 'finished'})
        body = res.get_json()
        self.assertEqual(body['status'], 'finished')
        self.assertIsNotNone(body['finishedAt'])
        # 结题生成里程碑反馈
        res = self._get(self.student, f'/api/projects/{pid}/feedbacks')
        feedbacks = res.get_json()['items']
        self.assertEqual(feedbacks[0]['type'], 'milestone')
        self.assertIn('结题', feedbacks[0]['content'])
        # 结题后档案可访问
        res = self._get(self.student, f'/api/projects/{pid}/archive')
        self.assertEqual(res.status_code, 200)

    def test_archive_requires_finished(self):
        res = self._get(self.student, '/api/projects/p1/archive')
        self.assert_error(res, 409, 'PROJECT_NOT_FINISHED')

    def test_archive_content(self):
        res = self._get(self.student, '/api/projects/p2/archive')
        body = res.get_json()
        self.assertEqual(body['summary']['taskTotal'], 4)
        self.assertEqual(body['summary']['doneTotal'], 4)
        self.assertEqual(body['summary']['durationDays'], 34)
        self.assertEqual(len(body['tasks']), 4)
        self.assertEqual(len(body['mindNodes']), 4)
        # 成员贡献统计
        u1 = next(m for m in body['members'] if m['user']['id'] == 'u1')
        self.assertEqual(u1['taskCount'], 2)
        self.assertEqual(u1['doneCount'], 2)

    # ------------------------------------------------------------ 星云看板

    def test_mind_node_crud(self):
        # 创建子节点
        res = self._post(self.student, '/api/projects/p1/mind-nodes', {'parentId': 'n1', 'label': '新分支'})
        self.assertEqual(res.status_code, 201)
        child = res.get_json()
        self.assertEqual(child['parentId'], 'n1')
        # 空标签 / 无效父节点 -> 400
        res = self._post(self.student, '/api/projects/p1/mind-nodes', {'parentId': 'n1', 'label': ''})
        self.assert_error(res, 400, 'VALIDATION_ERROR')
        res = self._post(self.student, '/api/projects/p1/mind-nodes', {'parentId': 'nope', 'label': 'x'})
        self.assert_error(res, 400, 'VALIDATION_ERROR')
        # 重命名
        res = self._patch(self.student, f"/api/mind-nodes/{child['id']}", {'label': '重命名分支'})
        self.assertEqual(res.get_json()['label'], '重命名分支')
        # 删除含子树：给子节点再加一个孙节点，删除子节点后两者都消失
        res = self._post(self.student, '/api/projects/p1/mind-nodes', {'parentId': child['id'], 'label': '孙节点'})
        grandchild = res.get_json()
        res = self._delete(self.student, f"/api/mind-nodes/{child['id']}")
        self.assertEqual(res.status_code, 204)
        items = self._get(self.student, '/api/projects/p1/mind-nodes').get_json()['items']
        ids = [n['id'] for n in items]
        self.assertNotIn(child['id'], ids)
        self.assertNotIn(grandchild['id'], ids)
        # 教师只读
        res = self._post(self.teacher, '/api/projects/p1/mind-nodes', {'parentId': 'n1', 'label': 'x'})
        self.assert_error(res, 403, 'FORBIDDEN')
        # 不存在 -> 404
        res = self._patch(self.student, '/api/mind-nodes/nope', {'label': 'x'})
        self.assert_error(res, 404, 'NOT_FOUND')

    def test_notes_crud(self):
        res = self._post(self.student, '/api/projects/p1/notes', {'content': '新灵感', 'x': 100, 'y': 200})
        self.assertEqual(res.status_code, 201)
        note = res.get_json()
        self.assertEqual(note['color'], '#fde68a')  # 默认颜色
        # 部分更新
        res = self._patch(self.student, f"/api/notes/{note['id']}", {'content': '改过的灵感', 'color': '#bbf7d0'})
        body = res.get_json()
        self.assertEqual(body['content'], '改过的灵感')
        self.assertEqual(body['color'], '#bbf7d0')
        self.assertEqual(body['x'], 100)  # 未提交字段保持不变
        # 删除
        res = self._delete(self.student, f"/api/notes/{note['id']}")
        self.assertEqual(res.status_code, 204)
        items = self._get(self.student, '/api/projects/p1/notes').get_json()['items']
        self.assertNotIn(note['id'], [n['id'] for n in items])
        # 不存在 -> 404
        res = self._delete(self.student, '/api/notes/nope')
        self.assert_error(res, 404, 'NOT_FOUND')

    # ------------------------------------------------------------ PBL 任务

    def test_create_task(self):
        task = self.create_task(self.student, 'p1', '新任务')
        self.assertEqual(task['status'], 'todo')
        self.assertIsNone(task['assigneeId'])
        # 自动追加 create 动态
        logs = self._get(self.student, '/api/projects/p1/task-logs').get_json()['items']
        self.assertEqual(logs[0]['taskId'], task['id'])
        self.assertEqual(logs[0]['action'], 'create')
        # 空标题 -> 400
        res = self._post(self.student, '/api/projects/p1/tasks', {'title': ''})
        self.assert_error(res, 400, 'VALIDATION_ERROR')

    def test_task_claim(self):
        task = self.create_task(self.student, 'p1')
        # 认领给自己
        res = self._patch(self.student, f"/api/tasks/{task['id']}", {'assigneeId': 'u1'})
        self.assertEqual(res.get_json()['assigneeId'], 'u1')
        # 认领他人 -> 403
        res = self._patch(self.student, f"/api/tasks/{task['id']}", {'assigneeId': 'u2'})
        self.assert_error(res, 403, 'FORBIDDEN')
        # 取消认领
        res = self._patch(self.student, f"/api/tasks/{task['id']}", {'assigneeId': None})
        self.assertIsNone(res.get_json()['assigneeId'])

    def test_task_status_flow_creates_checkin_and_feedback(self):
        task = self.create_task(self.student, 'p1')
        self.complete_task(task['id'])
        # 动态（按时间倒序）：3 次 status + create
        logs = self._get(self.student, '/api/projects/p1/task-logs').get_json()['items']
        task_logs = [l for l in logs if l['taskId'] == task['id']]
        self.assertEqual([l['action'] for l in task_logs], ['status', 'status', 'status', 'claim', 'create'])
        # 完成时自动生成打卡 + 里程碑反馈
        checkins = self._get(self.student, '/api/projects/p1/checkins').get_json()['items']
        auto = next(c for c in checkins if c['userId'] == 'u1' and '里程碑任务' in c['content'])
        self.assertEqual(auto['content'], f"完成里程碑任务「{task['title']}」")
        feedbacks = self._get(self.student, '/api/projects/p1/feedbacks').get_json()['items']
        self.assertEqual(feedbacks[0]['type'], 'milestone')
        self.assertIn(feedbacks[0]['content'], FEEDBACK_POOL)
        # 非法状态 -> 400
        res = self._patch(self.student, f"/api/tasks/{task['id']}", {'status': 'nope'})
        self.assert_error(res, 400, 'VALIDATION_ERROR')

    def test_task_filters_and_delete(self):
        res = self._get(self.student, '/api/projects/p1/tasks?status=done')
        self.assertTrue(all(t['status'] == 'done' for t in res.get_json()['items']))
        res = self._get(self.student, '/api/projects/p1/tasks?assigneeId=u1')
        self.assertTrue(all(t['assigneeId'] == 'u1' for t in res.get_json()['items']))
        # 删除任务并记录 delete 动态
        task = self.create_task(self.student, 'p1')
        res = self._delete(self.student, f"/api/tasks/{task['id']}")
        self.assertEqual(res.status_code, 204)
        logs = self._get(self.student, '/api/projects/p1/task-logs').get_json()['items']
        self.assertEqual(logs[0]['action'], 'delete')
        self.assertIn('删除任务', logs[0]['detail'])
        # 不存在 -> 404 TASK_NOT_FOUND
        res = self._patch(self.student, '/api/tasks/nope', {'status': 'done'})
        self.assert_error(res, 404, 'TASK_NOT_FOUND')

    # ------------------------------------------------------------ 打卡与反馈

    def test_checkin_creates_guide_feedback(self):
        before = self._get(self.student, '/api/projects/p1/feedbacks').get_json()['total']
        res = self._post(self.student, '/api/projects/p1/checkins', {'content': '今天完成了模型搭建'})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()['userId'], 'u1')
        after = self._get(self.student, '/api/projects/p1/feedbacks').get_json()['items']
        self.assertEqual(len(after), before + 1)
        self.assertEqual(after[0]['type'], 'guide')
        # 空内容 -> 400
        res = self._post(self.student, '/api/projects/p1/checkins', {'content': '  '})
        self.assert_error(res, 400, 'VALIDATION_ERROR')

    # ------------------------------------------------------------ 资源

    def test_resources_filter(self):
        res = self._get(self.student, '/api/resources?category=物理')
        items = res.get_json()['items']
        self.assertEqual(len(items), 3)
        self.assertTrue(all(r['category'] == '物理' for r in items))
        # 关键词大小写不敏感，匹配标题/描述/标签
        res = self._get(self.student, '/api/resources?keyword=ai')
        titles = [r['title'] for r in res.get_json()['items']]
        self.assertIn('Teachable Machine', titles)  # 标签 AI
        res = self._get(self.student, '/api/resources?keyword=python')
        titles = [r['title'] for r in res.get_json()['items']]
        self.assertIn('Codecademy Python 课程', titles)
        # 组合过滤
        res = self._get(self.student, '/api/resources?category=工程&keyword=nasa')
        titles = [r['title'] for r in res.get_json()['items']]
        self.assertEqual(titles, ['NASA 开放数据平台'])

    # ------------------------------------------------------------ 专注模式

    def test_focus_sessions(self):
        res = self._post(self.student, '/api/focus-sessions', {'durationMin': 25, 'type': 'focus'})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()['durationMin'], 25)
        res = self._post(self.student, '/api/focus-sessions', {'durationMin': 5, 'type': 'break'})
        self.assertEqual(res.get_json()['type'], 'break')
        # 非法时长 -> 400
        for bad in (0, -5, 'abc'):
            res = self._post(self.student, '/api/focus-sessions', {'durationMin': bad})
            self.assert_error(res, 400, 'VALIDATION_ERROR')
        # 列表按时间倒序
        res = self._get(self.student, '/api/focus-sessions')
        items = res.get_json()['items']
        self.assertGreaterEqual(len(items), 2)
        self.assertEqual(items[0]['type'], 'break')

    def test_focus_stats(self):
        res = self._get(self.student, '/api/focus/stats?days=3')
        body = res.get_json()
        self.assertEqual(len(body['week']), 3)
        # 今天的记录计入 today
        self._post(self.student, '/api/focus-sessions', {'durationMin': 25, 'type': 'focus'})
        self._post(self.student, '/api/focus-sessions', {'durationMin': 25, 'type': 'focus'})
        res = self._get(self.student, '/api/focus/stats?days=7')
        body = res.get_json()
        self.assertEqual(body['today'], {'count': 2, 'minutes': 50})
        # week 按日期升序，最后一格是今天
        dates = [d['date'] for d in body['week']]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(body['week'][-1]['count'], 2)
        # days 上限 30
        res = self._get(self.student, '/api/focus/stats?days=999')
        self.assertEqual(len(res.get_json()['week']), 30)

    # ------------------------------------------------------------ 教师端

    def test_teacher_projects(self):
        res = self._get(self.student, '/api/teacher/projects')
        self.assert_error(res, 403, 'FORBIDDEN')
        res = self._get(self.teacher, '/api/teacher/projects')
        self.assertEqual(res.status_code, 200)
        ids = [p['id'] for p in res.get_json()['items']]
        self.assertEqual(ids, ['p1', 'p2'])  # 按最近更新倒序

    def test_annotations(self):
        # 学生只读
        res = self._get(self.student, '/api/projects/p2/annotations')
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(res.get_json()['total'], 3)
        # 教师添加
        res = self._post(self.teacher, '/api/projects/p2/annotations', {'content': '新批注'})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()['userId'], 't1')
        # 学生添加 -> 403
        res = self._post(self.student, '/api/projects/p2/annotations', {'content': 'x'})
        self.assert_error(res, 403, 'FORBIDDEN')
        # 空内容 -> 400
        res = self._post(self.teacher, '/api/projects/p2/annotations', {'content': ''})
        self.assert_error(res, 400, 'VALIDATION_ERROR')

    def test_teacher_read_only_on_collab(self):
        # 教师不能编辑学生的任务内容，但可以验收。
        res = self._post(self.teacher, '/api/projects/p1/tasks', {'title': 'x'})
        self.assert_error(res, 403, 'FORBIDDEN')
        res = self._post(self.teacher, '/api/projects/p1/checkins', {'content': 'x'})
        self.assert_error(res, 403, 'FORBIDDEN')
        res = self._patch(self.teacher, '/api/tasks/t1', {'title': 'x'})
        self.assert_error(res, 403, 'FORBIDDEN')
        res = self._post(self.teacher, '/api/projects/p1/notes', {'content': 'x'})
        self.assert_error(res, 403, 'FORBIDDEN')
        # 但可以读
        res = self._get(self.teacher, '/api/projects/p1/tasks')
        self.assertEqual(res.status_code, 200)

    # ------------------------------------------------------------ 知识闯关

    def test_quiz_questions(self):
        res = self.client.get('/api/quiz/questions')
        self.assert_error(res, 401, 'UNAUTHORIZED')
        res = self._get(self.student, '/api/quiz/questions?count=5')
        body = res.get_json()
        self.assertEqual(len(body['items']), 5)
        self.assertGreaterEqual(body['total'], 20)
        q = body['items'][0]
        self.assertEqual(
            set(q),
            {'id', 'groupId', 'createdBy', 'createdAt', 'updatedAt', 'category', 'difficulty',
             'question', 'options'})
        self.assertIsNone(q['groupId'])
        self.assertEqual(len(q['options']), 4)
        self.assertIsInstance(body['roundId'], str)
        # count 上限 20，非法值回落默认 10
        res = self._get(self.student, '/api/quiz/questions?count=999')
        self.assertEqual(len(res.get_json()['items']), 20)
        res = self._get(self.student, '/api/quiz/questions?count=abc')
        self.assertEqual(len(res.get_json()['items']), 10)

    def test_quiz_attempts_and_stats(self):
        res = self.play_round(self.student, 8)
        self.assertEqual(res.status_code, 201)
        body = res.get_json()
        self.assertEqual(body['attempt']['score'], 80)
        self.assertEqual(body['best']['score'], 80)
        res = self.play_round(self.student, 10)
        self.assertEqual(res.get_json()['best']['score'], 100)
        stats = self._get(self.student, '/api/quiz/stats').get_json()
        self.assertEqual(stats['attempts'], 2)
        self.assertEqual(stats['best']['score'], 100)
        self.assertEqual(stats['last']['score'], 100)
        # 不同用户成绩互不干扰
        self.play_round(self.student2, 2)
        stats = self._get(self.student, '/api/quiz/stats').get_json()
        self.assertEqual(stats['attempts'], 2)

    def test_quiz_attempt_validation(self):
        for bad in ({'score': '8', 'total': 10}, {'score': 11, 'total': 10}, {'score': 5}, {'score': -1, 'total': 10}):
            res = self._post(self.student, '/api/quiz/attempts', bad)
            self.assert_error(res, 400, 'VALIDATION_ERROR')
        stats = self._get(self.student, '/api/quiz/stats').get_json()
        self.assertIsNone(stats['best'])
        self.assertIsNone(stats['last'])

    # ------------------------------------------------------------ 用户组与题库

    def test_group_crud(self):
        res = self._post(self.student, '/api/groups', {'name': '测试组'})
        self.assert_error(res, 403, 'FORBIDDEN')
        res = self._post(self.teacher, '/api/groups', {'name': '测试组', 'description': '描述', 'quizMode': 'mixed'})
        self.assertEqual(res.status_code, 201)
        g = res.get_json()
        self.assertEqual(g['name'], '测试组')
        self.assertEqual(g['quizMode'], 'mixed')
        self.assertEqual(g['memberCount'], 1)  # 创建者自动成为负责老师
        gid = g['id']
        # 非法 quizMode -> 400
        res = self._post(self.teacher, '/api/groups', {'name': '坏组', 'quizMode': 'xxx'})
        self.assert_error(res, 400, 'VALIDATION_ERROR')
        res = self._patch(self.teacher, f'/api/groups/{gid}', {'name': '测试组2', 'quizMode': 'fallback'})
        body = res.get_json()
        self.assertEqual(body['name'], '测试组2')
        self.assertEqual(body['quizMode'], 'fallback')
        self.assertEqual(len(self._get(self.teacher, '/api/groups').get_json()['items']), 2)  # g1 + 新组
        # 非管理教师不可改名
        t2 = self.register_teacher()
        res = self._patch(t2, f'/api/groups/{gid}', {'name': 'x'})
        self.assert_error(res, 403, 'FORBIDDEN')
        # 删除连带清空成员与组内题目
        self._post(self.teacher, f'/api/groups/{gid}/members', {'userId': 'u4', 'role': 'member'})
        res = self._delete(self.teacher, f'/api/groups/{gid}')
        self.assertEqual(res.status_code, 204)
        res = self._get(self.teacher, f'/api/groups/{gid}/members')
        self.assert_error(res, 404, 'GROUP_NOT_FOUND')

    def test_group_members(self):
        gid = self._post(self.teacher, '/api/groups', {'name': '成员测试组'}).get_json()['id']
        # 添加学生与第二个负责老师
        res = self._post(self.teacher, f'/api/groups/{gid}/members', {'userId': 'u4', 'role': 'member'})
        self.assertEqual(res.status_code, 201)
        t2 = self.register_teacher()
        res = self._post(self.teacher, f'/api/groups/{gid}/members', {'userId': t2['user']['id'], 'role': 'teacher'})
        self.assertEqual(res.status_code, 201)
        # 重复添加 -> 409
        res = self._post(self.teacher, f'/api/groups/{gid}/members', {'userId': 'u4', 'role': 'member'})
        self.assert_error(res, 409, 'ALREADY_MEMBER')
        # 不存在的用户 -> 404
        res = self._post(self.teacher, f'/api/groups/{gid}/members', {'userId': 'nobody', 'role': 'member'})
        self.assert_error(res, 404, 'NOT_FOUND')
        # 学生可同时在多个组（u4 已在 gid，再加入 g2）
        g2 = self._post(self.teacher, '/api/groups', {'name': '第二组'}).get_json()['id']
        self._post(self.teacher, f'/api/groups/{g2}/members', {'userId': 'u4', 'role': 'member'})
        mine = self._get(self.student4, '/api/groups/mine').get_json()['items']
        self.assertEqual(len(mine), 2)
        # 非管理教师不可加人
        res = self._post(t2, f'/api/groups/{g2}/members', {'userId': 'u2', 'role': 'member'})
        self.assert_error(res, 403, 'FORBIDDEN')
        # 移除最后一个负责老师被拒
        res = self._delete(self.teacher, f'/api/groups/{g2}/members/t1')
        self.assert_error(res, 400, 'VALIDATION_ERROR')
        # 移除不存在的成员 -> 404
        res = self._delete(self.teacher, f'/api/groups/{gid}/members/u2')
        self.assert_error(res, 404, 'NOT_FOUND')
        # 正常移除成员
        res = self._delete(self.teacher, f'/api/groups/{gid}/members/u4')
        self.assertEqual(res.status_code, 204)
        members = self._get(self.teacher, f'/api/groups/{gid}/members').get_json()['items']
        self.assertNotIn('u4', [m['userId'] for m in members])

    def test_group_questions_crud(self):
        gid = self._post(self.teacher, '/api/groups', {'name': '题库测试组'}).get_json()['id']
        body = {
            'question': '测试题：火星日长约多少？', 'category': '物理', 'difficulty': 2,
            'options': ['24 小时', '24 小时 39 分', '25 小时', '23 小时'], 'answer': 1,
            'explanation': '火星一个太阳日约 24 小时 39 分。',
        }
        res = self._post(self.teacher, f'/api/groups/{gid}/questions', body)
        self.assertEqual(res.status_code, 201)
        q = res.get_json()
        self.assertEqual(q['groupId'], gid)
        self.assertEqual(q['options'][q['answer']], '24 小时 39 分')
        self.assertEqual(q['createdBy'], 't1')
        for bad in (
            {**body, 'options': ['a', 'b']},
            {**body, 'answer': 4},
            {**body, 'question': '  '},
            {**body, 'difficulty': 5},
            {**body, 'explanation': ''},
        ):
            res = self._post(self.teacher, f'/api/groups/{gid}/questions', bad)
            self.assert_error(res, 400, 'VALIDATION_ERROR')
        # 非管理教师不可出题/改题
        t2 = self.register_teacher()
        res = self._post(t2, f'/api/groups/{gid}/questions', body)
        self.assert_error(res, 403, 'FORBIDDEN')
        qid = q['id']
        res = self._patch(t2, f'/api/groups/{gid}/questions/{qid}', body)
        self.assert_error(res, 403, 'FORBIDDEN')
        # 更新与删除
        res = self._patch(self.teacher, f'/api/groups/{gid}/questions/{qid}', {**body, 'question': '改过的题'})
        self.assertEqual(res.get_json()['question'], '改过的题')
        res = self._delete(self.teacher, f'/api/groups/{gid}/questions/{qid}')
        self.assertEqual(res.status_code, 204)
        items = self._get(self.teacher, f'/api/groups/{gid}/questions').get_json()['items']
        self.assertEqual(items, [])

    def test_quiz_questions_group_modes(self):
        # g1 默认 fallback：组内 5 题，不混公共题
        res = self._get(self.student, '/api/quiz/questions?group=g1&count=10')
        body = res.get_json()
        self.assertEqual(len(body['items']), 5)
        self.assertEqual(body['group'], {'id': 'g1', 'name': '火星能源课题小组'})
        self.assertTrue(all(q['groupId'] == 'g1' for q in body['items']))
        # 非成员玩别人的组 -> 403
        res = self._get(self.student4, '/api/quiz/questions?group=g1')
        self.assert_error(res, 403, 'FORBIDDEN')
        # 未分组学生 -> 公共题库
        res = self._get(self.student4, '/api/quiz/questions?count=5')
        body = res.get_json()
        self.assertEqual(len(body['items']), 5)
        self.assertIsNone(body['group'])
        self.assertTrue(all(q['groupId'] is None for q in body['items']))
        # group 模式且组内为空 -> 空题库
        gid = self._post(self.teacher, '/api/groups', {'name': '空题库组'}).get_json()['id']
        self._post(self.teacher, f'/api/groups/{gid}/members', {'userId': 'u4', 'role': 'member'})
        res = self._get(self.student4, f'/api/quiz/questions?group={gid}')
        self.assertEqual(res.get_json()['items'], [])
        # fallback 模式且组内为空 -> 回退公共题库
        self._patch(self.teacher, f'/api/groups/{gid}', {'quizMode': 'fallback'})
        res = self._get(self.student4, f'/api/quiz/questions?group={gid}&count=50')
        items = res.get_json()['items']
        self.assertEqual(len(items), 20)
        self.assertTrue(all(q['groupId'] is None for q in items))
        # mixed 模式 -> 组内与公共混合
        self.assertEqual(self._post(self.teacher, f'/api/groups/{gid}/questions', {
            'question': '混合模式测试题', 'category': '综合', 'difficulty': 1,
            'options': ['A1', 'A2', 'A3', 'A4'], 'answer': 0, 'explanation': '混合模式说明',
        }).status_code, 201)
        self.assertEqual(self._patch(self.teacher, f'/api/groups/{gid}', {'quizMode': 'mixed'}).status_code, 200)
        res = self._get(self.student4, f'/api/quiz/questions?group={gid}&count=50')
        body = res.get_json()
        self.assertEqual(len(body['items']), 20)  # 单次抽取上限 20
        self.assertEqual(body['total'], 21)  # 20 公共 + 1 组内，证明混合
        self.assertTrue(all(q['groupId'] in (None, gid) for q in body['items']))

    def test_user_search(self):
        res = self._get(self.student, '/api/users?keyword=张')
        self.assert_error(res, 403, 'FORBIDDEN')
        res = self._get(self.teacher, '/api/users?keyword=张')
        ids = [u['id'] for u in res.get_json()['items']]
        self.assertIn('u1', ids)
        res = self._get(self.teacher, '/api/users?keyword=不存在的名字')
        self.assertEqual(res.get_json()['items'], [])

    def test_group_join_by_code(self):
        # 教师不能通过邀请码入组
        res = self._post(self.teacher, '/api/groups/join', {'inviteCode': 'G1-KM3X'})
        self.assert_error(res, 403, 'FORBIDDEN')
        # 无效邀请码
        res = self._post(self.student4, '/api/groups/join', {'inviteCode': 'G9-XXXX'})
        self.assert_error(res, 409, 'INVALID_INVITE')
        # 未分组学生凭码入组（大小写不敏感）
        res = self._post(self.student4, '/api/groups/join', {'inviteCode': 'g1-km3x'})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()['id'], 'g1')
        # 已在组内
        res = self._post(self.student4, '/api/groups/join', {'inviteCode': 'G1-KM3X'})
        self.assert_error(res, 409, 'ALREADY_MEMBER')
        # mine 现在包含 g1（含统计字段，不暴露邀请码）
        mine = self._get(self.student4, '/api/groups/mine').get_json()['items']
        self.assertIn('g1', [g['id'] for g in mine])
        self.assertNotIn('inviteCode', mine[0])
        self.assertIn('quizMode', mine[0])
        self.assertIn('questionCount', mine[0])
        # 新建的组会自动生成邀请码
        self._post(self.teacher, '/api/groups', {'name': '新组'})
        self.assertTrue(self._get(self.teacher, '/api/groups').get_json()['items'][0]['inviteCode'].startswith('G'))

    def test_group_invite_flow(self):
        res = self._post(self.teacher, '/api/groups/g1/invites', {'userId': 'u4'})
        self.assertEqual(res.status_code, 201)
        invite_id = res.get_json()['id']
        # 重复发送 -> 409
        res = self._post(self.teacher, '/api/groups/g1/invites', {'userId': 'u4'})
        self.assert_error(res, 409, 'ALREADY_INVITED')
        # 目标为老师 -> 400
        res = self._post(self.teacher, '/api/groups/g1/invites', {'userId': 't1'})
        self.assert_error(res, 400, 'VALIDATION_ERROR')
        # 非管理教师不能发邀请
        t2 = self.register_teacher()
        res = self._post(t2, '/api/groups/g1/invites', {'userId': 'u4'})
        self.assert_error(res, 403, 'FORBIDDEN')
        # 学生端看到待处理邀请（含组名与邀请老师）
        invites = self._get(self.student4, '/api/groups/invites').get_json()['items']
        self.assertEqual(len(invites), 1)
        self.assertEqual(invites[0]['groupName'], '火星能源课题小组')
        self.assertEqual(invites[0]['inviterName'], '王老师')
        # 老师端看到邀请中
        pending = self._get(self.teacher, '/api/groups/g1/invites').get_json()['items']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['username'], 'student4')
        # 通过 -> 入组
        res = self._post(self.student4, f'/api/groups/invites/{invite_id}/respond', {'accept': True})
        self.assertEqual(res.get_json()['status'], 'accepted')
        mine = self._get(self.student4, '/api/groups/mine').get_json()['items']
        self.assertIn('g1', [g['id'] for g in mine])
        # 已处理邀请不再出现
        invites = self._get(self.student4, '/api/groups/invites').get_json()['items']
        self.assertEqual(invites, [])
        # 非布尔 accept -> 400
        res = self.client.post('/api/users', json={
            'username': 'newbie', 'password': '123456', 'name': '新人', 'role': 'student',
        })
        self.assertEqual(res.status_code, 201)
        newbie = self._login('newbie', '123456')
        res = self._post(self.teacher, '/api/groups/g1/invites', {'userId': newbie['user']['id']})
        invite_id2 = res.get_json()['id']
        res = self._post(newbie, f'/api/groups/invites/{invite_id2}/respond', {'accept': 'yes'})
        self.assert_error(res, 400, 'VALIDATION_ERROR')
        # 不能替他人处理邀请
        res = self._post(self.student, f'/api/groups/invites/{invite_id2}/respond', {'accept': True})
        self.assert_error(res, 404, 'NOT_FOUND')

    def test_group_invite_decline_and_withdraw(self):
        # 拒绝 -> 不入组
        res = self._post(self.teacher, '/api/groups/g1/invites', {'userId': 'u4'})
        invite_id = res.get_json()['id']
        res = self._post(self.student4, f'/api/groups/invites/{invite_id}/respond', {'accept': False})
        self.assertEqual(res.get_json()['status'], 'declined')
        mine = self._get(self.student4, '/api/groups/mine').get_json()['items']
        self.assertNotIn('g1', [g['id'] for g in mine])
        # 重复处理 -> 404
        res = self._post(self.student4, f'/api/groups/invites/{invite_id}/respond', {'accept': True})
        self.assert_error(res, 404, 'NOT_FOUND')
        # 老师撤回
        res = self._post(self.teacher, '/api/groups/g1/invites', {'userId': 'u4'})
        invite_id = res.get_json()['id']
        res = self._delete(self.teacher, f'/api/groups/g1/invites/{invite_id}')
        self.assertEqual(res.status_code, 204)
        pending = self._get(self.teacher, '/api/groups/g1/invites').get_json()['items']
        self.assertEqual(pending, [])
        # 撤回已处理的邀请 -> 404
        res = self._delete(self.teacher, f'/api/groups/g1/invites/{invite_id}')
        self.assert_error(res, 404, 'NOT_FOUND')

    def test_project_group_scoping(self):
        # 学生创建项目自动归入所在组（u1 在 g1）
        p = self.create_project(self.student, 'topic2', '组内新项目')
        self.assertEqual(p['groupId'], 'g1')
        self.assertEqual(p['group'], {'id': 'g1', 'name': '火星能源课题小组'})
        # 未分组学生创建 -> 公共项目
        p2 = self.create_project(self.student4, 'topic2', '公共项目')
        self.assertIsNone(p2['groupId'])
        # 同组成员（未加入）可见组内项目；跨组不可见
        res = self._get(self.student2, '/api/projects')
        ids = [x['id'] for x in res.get_json()['items']]
        self.assertIn(p['id'], ids)
        self.assertIn(p2['id'], ids)  # 公共项目也可见
        res = self._get(self.student4, '/api/projects')
        ids = [x['id'] for x in res.get_json()['items']]
        self.assertNotIn('p1', ids)
        self.assertIn(p2['id'], ids)
        # 跨组详情 -> 403
        res = self._get(self.student4, '/api/projects/p1')
        self.assert_error(res, 403, 'FORBIDDEN')
        # 同组一键加入
        res = self._post(self.student2, f'/api/projects/{p["id"]}/join')
        self.assertEqual(res.status_code, 201)
        # 重复加入 -> 409
        res = self._post(self.student2, f'/api/projects/{p["id"]}/join')
        self.assert_error(res, 409, 'ALREADY_MEMBER')
        # 跨组一键加入 -> 403
        res = self._post(self.student4, f'/api/projects/{p["id"]}/join')
        self.assert_error(res, 403, 'FORBIDDEN')
        # 教师一键加入 -> 403
        res = self._post(self.teacher, f'/api/projects/{p2["id"]}/join')
        self.assert_error(res, 403, 'FORBIDDEN')
        # 公共项目任意学生可加入
        res = self._post(self.student2, f'/api/projects/{p2["id"]}/join')
        self.assertEqual(res.status_code, 201)

    def test_teacher_projects_group_filter(self):
        # 默认：我管理的组 + 公共
        res = self._get(self.teacher, '/api/teacher/projects')
        ids = [p['id'] for p in res.get_json()['items']]
        self.assertEqual(ids, ['p1', 'p2'])
        # 按组筛选
        res = self._get(self.teacher, '/api/teacher/projects?group=g1')
        ids = [p['id'] for p in res.get_json()['items']]
        self.assertEqual(ids, ['p1', 'p2'])
        # 非管理的组 -> 403
        gid = self._post(self.teacher, '/api/groups', {'name': '新组'}).get_json()['id']
        t2 = self.register_teacher()
        res = self._get(t2, f'/api/teacher/projects?group={gid}')
        self.assert_error(res, 403, 'FORBIDDEN')
        # 学生访问 -> 403
        res = self._get(self.student, '/api/teacher/projects?group=g1')
        self.assert_error(res, 403, 'FORBIDDEN')

    # ------------------------------------------------------------ 用户管理（管理角色）

    def test_admin_user_management_scopes(self):
        # 非管理角色 -> 403
        res = self._get(self.student, '/api/admin/users')
        self.assert_error(res, 403, 'FORBIDDEN')
        res = self._get(self.teacher, '/api/admin/users')
        self.assert_error(res, 403, 'FORBIDDEN')
        # 校管理员：只能看到老师/学生
        items = self._get(self.schooladmin, '/api/admin/users').get_json()['items']
        roles = {u['role'] for u in items}
        self.assertEqual(roles, {'student', 'teacher'})
        self.assertNotIn('schooladmin', roles)
        # 管理员：能看到校管理员及以下
        roles = {u['role'] for u in self._get(self.admin, '/api/admin/users').get_json()['items']}
        self.assertEqual(roles, {'student', 'teacher', 'schooladmin'})
        # 超级管理员：能看到管理员及以下
        roles = {u['role'] for u in self._get(self.superadmin, '/api/admin/users').get_json()['items']}
        self.assertEqual(roles, {'student', 'teacher', 'schooladmin', 'admin'})
        # 均不含自己
        ids = [u['id'] for u in self._get(self.superadmin, '/api/admin/users').get_json()['items']]
        self.assertNotIn('sa1', ids)

    def test_admin_user_create_update_delete(self):
        # 校管理员不能创建校管理员及以上
        res = self._post(self.schooladmin, '/api/admin/users',
                         {'username': 'sc2', 'password': '123456', 'name': '新校管', 'role': 'schooladmin'})
        self.assert_error(res, 400, 'VALIDATION_ERROR')
        # 管理员可创建校管理员，超级管理员可创建管理员
        res = self._post(self.admin, '/api/admin/users',
                         {'username': 'sc2', 'password': '123456', 'name': '新校管', 'role': 'schooladmin'})
        self.assertEqual(res.status_code, 201)
        sc2 = res.get_json()
        self.assertEqual(sc2['role'], 'schooladmin')
        res = self._post(self.superadmin, '/api/admin/users',
                         {'username': 'ad2', 'password': '123456', 'name': '新管理员', 'role': 'admin'})
        self.assertEqual(res.status_code, 201)
        ad2 = res.get_json()
        # 超级管理员不能创建超级管理员
        res = self._post(self.superadmin, '/api/admin/users',
                         {'username': 'sa2', 'password': '123456', 'name': '新超管', 'role': 'superadmin'})
        self.assert_error(res, 400, 'VALIDATION_ERROR')
        # 改名/重置密码/改角色
        res = self._patch(self.superadmin, f"/api/admin/users/{ad2['id']}", {'name': '管理员二号', 'role': 'schooladmin'})
        self.assertEqual(res.get_json()['name'], '管理员二号')
        self.assertEqual(res.get_json()['role'], 'schooladmin')
        # 同级管理员不可互管（ad3 由超管创建，admin 不能改）
        res = self._post(self.superadmin, '/api/admin/users',
                         {'username': 'ad3', 'password': '123456', 'name': '三号管理员', 'role': 'admin'})
        ad3 = res.get_json()
        res = self._patch(self.admin, f"/api/admin/users/{ad3['id']}", {'name': 'x'})
        self.assert_error(res, 403, 'FORBIDDEN')
        # 不能改自己
        res = self._patch(self.superadmin, '/api/admin/users/sa1', {'name': 'x'})
        self.assert_error(res, 400, 'VALIDATION_ERROR')
        # 校管理员不能调整校管理员/管理员账号
        res = self._patch(self.schooladmin, f"/api/admin/users/{sc2['id']}", {'name': 'x'})
        self.assert_error(res, 403, 'FORBIDDEN')
        # 校管理员可调整老师
        res = self._patch(self.schooladmin, '/api/admin/users/t1', {'role': 'student', 'password': 'abcdef'})
        self.assertEqual(res.get_json()['role'], 'student')
        # 重置后的密码可登录
        login = self.client.post('/api/sessions', json={'username': 'teacher', 'password': 'abcdef'})
        self.assertEqual(login.status_code, 201)
        self.assertEqual(login.get_json()['user']['role'], 'student')
        # 删除：管理员删除校管理员，级联生效
        res = self._delete(self.admin, f"/api/admin/users/{sc2['id']}")
        self.assertEqual(res.status_code, 204)
        res = self._get(self.admin, '/api/admin/users')
        self.assertNotIn(sc2['id'], [u['id'] for u in res.get_json()['items']])
        # 删除更高层 -> 403
        res = self._delete(self.admin, '/api/admin/users/sa1')
        self.assert_error(res, 403, 'FORBIDDEN')

    def test_admin_delete_cascade(self):
        # u4 加入 g1、加入 p2、有专注记录，删除后关联数据清理
        self._post(self.teacher, '/api/groups/g1/members', {'userId': 'u4', 'role': 'member'})
        self._post(self.student4, '/api/focus-sessions', {'durationMin': 25, 'type': 'focus'})
        res = self._delete(self.schooladmin, '/api/admin/users/u4')
        self.assertEqual(res.status_code, 204)
        res = self._get(self.schooladmin, '/api/admin/users')
        self.assertNotIn('u4', [u['id'] for u in res.get_json()['items']])
        # u4 的组成员关系与专注记录已级联删除
        members = self._get(self.teacher, '/api/groups/g1/members').get_json()['items']
        self.assertNotIn('u4', [m['userId'] for m in members])
        items = self._get(self.schooladmin, '/api/focus-sessions').get_json()['items']
        self.assertEqual(items, [])
        # 项目成员同步移除
        res = self._get(self.teacher, '/api/projects/p2')
        self.assertNotIn('u4', [m['id'] for m in res.get_json()['members']])

    def test_schooladmin_manages_any_group(self):
        # 校管理员可管理任意组（g1）与建新组
        gid = self._post(self.schooladmin, '/api/groups', {'name': '校管建的组'}).get_json()['id']
        res = self._post(self.schooladmin, f'/api/groups/{gid}/members', {'userId': 'u4', 'role': 'member'})
        self.assertEqual(res.status_code, 201)
        res = self._post(self.schooladmin, '/api/groups/g1/members', {'userId': 'u4', 'role': 'member'})
        self.assertEqual(res.status_code, 201)
        res = self._post(self.schooladmin, f'/api/groups/{gid}/questions', {
            'question': '校管出的题', 'category': '综合', 'difficulty': 1,
            'options': ['A1', 'A2', 'A3', 'A4'], 'answer': 0, 'explanation': '校管解析',
        })
        self.assertEqual(res.status_code, 201)
        # 列表返回全部组
        groups = self._get(self.schooladmin, '/api/groups').get_json()['items']
        self.assertEqual(len(groups), 2)
        # 团队总览可见全部项目
        ids = [p['id'] for p in self._get(self.schooladmin, '/api/teacher/projects').get_json()['items']]
        self.assertEqual(ids, ['p1', 'p2'])

    def test_register_role_still_limited(self):
        res = self.client.post('/api/users', json={
            'username': 'badadmin', 'password': '123456', 'name': '坏管理员', 'role': 'admin',
        })
        self.assert_error(res, 400, 'VALIDATION_ERROR')

    # ------------------------------------------------------------ 通用约定

    def test_pagination_shape(self):
        res = self._get(self.student, '/api/topics')
        body = res.get_json()
        self.assertEqual(set(body.keys()), {'items', 'total', 'page', 'pageSize'})
        # 显式分页
        res = self._get(self.student, '/api/resources?page=1&pageSize=2')
        body = res.get_json()
        self.assertEqual(len(body['items']), 2)
        self.assertEqual(body['page'], 1)
        self.assertEqual(body['pageSize'], 2)

    def test_unknown_route(self):
        res = self._get(self.student, '/api/does-not-exist')
        self.assert_error(res, 404, 'NOT_FOUND')

    def test_quiz_round_integrity_and_retry(self):
        data = self._get(self.student, '/api/quiz/questions?count=1').get_json()
        rid, q = data['roundId'], data['items'][0]
        self.assertNotIn('answer', q)
        self.assertNotIn('explanation', q)
        path = f'/api/quiz/rounds/{rid}/answers'
        self.assert_error(self._post(self.student2, path, {'questionId': q['id'], 'choice': 0}), 404, 'ROUND_NOT_FOUND')
        self.assert_error(self._post(self.student, '/api/quiz/attempts', {'roundId': rid}), 409, 'ROUND_INCOMPLETE')
        for body in ({'questionId': 'other', 'choice': 0}, {'questionId': q['id'], 'choice': True},
                     {'questionId': q['id'], 'choice': 99}):
            self.assert_error(self._post(self.student, path, body), 400, 'VALIDATION_ERROR')
        with self.app.app_context():
            correct = query_one('SELECT answer FROM quiz_questions WHERE id = ?', (q['id'],))['answer']
            # Editing the teacher's bank must not change a round already issued.
            execute('UPDATE quiz_questions SET answer = ? WHERE id = ?', ((correct + 1) % 4, q['id']))
            commit()
        body = {'questionId': q['id'], 'choice': correct}
        result = self._post(self.student, path, body).get_json()
        self.assertTrue(result['correct'])
        self.assertEqual(result['score'], 10)
        self.assertEqual(self._post(self.student, path, body).get_json(), result)
        self.assert_error(self._post(self.student, path, {**body, 'choice': (correct + 1) % 4}), 409, 'ANSWER_LOCKED')
        first = self._post(self.student, '/api/quiz/attempts', {'roundId': rid})
        self.assertEqual(first.status_code, 201)
        retry = self._post(self.student, '/api/quiz/attempts', {'roundId': rid})
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(first.get_json(), retry.get_json())
        self.assertEqual(self._get(self.student, '/api/quiz/stats').get_json()['attempts'], 1)

    def test_quiz_expiry_and_spoofed_score(self):
        data = self._get(self.student, '/api/quiz/questions?count=1').get_json()
        rid = data['roundId']
        self.assert_error(self._post(self.student, '/api/quiz/attempts', {'roundId': rid, 'score': 100}), 400, 'VALIDATION_ERROR')
        with self.app.app_context():
            execute("UPDATE quiz_rounds SET expiresAt = '2000-01-01T00:00:00Z' WHERE id = ?", (rid,))
            commit()
        self.assert_error(self._post(self.student, '/api/quiz/attempts', {'roundId': rid}), 409, 'ROUND_EXPIRED')

    def test_task_workflow_and_ownership(self):
        task = self.create_task(self.student, 'p1')
        path = f"/api/tasks/{task['id']}"
        self.assert_error(self._patch(self.student, path, {'status': 'done'}), 409, 'INVALID_TRANSITION')
        self.assert_error(self._patch(self.student, path, {'status': 'doing'}), 409, 'ASSIGNEE_REQUIRED')
        self.assertEqual(self._patch(self.student, path, {'assigneeId': 'u1'}).status_code, 200)
        for body in ({'assigneeId': 'u2'}, {'assigneeId': None}, {'title': 'take over'}):
            self.assert_error(self._patch(self.student2, path, body), 403, 'FORBIDDEN')
        self.assertEqual(self._patch(self.student, path, {'status': 'doing'}).status_code, 200)
        self.assert_error(self._patch(self.student, path, {'assigneeId': None}), 409, 'TASK_IN_PROGRESS')
        self.assertEqual(self._patch(self.student, path, {'status': 'review'}).status_code, 200)
        self.assert_error(self._patch(self.student, path, {'status': 'done'}), 409, 'INVALID_TRANSITION')
        self.assertEqual(self._patch(self.teacher, path, {'status': 'doing'}).status_code, 200)
        self.assertEqual(self._patch(self.student, path, {'status': 'review'}).status_code, 200)
        self.assertEqual(self._patch(self.teacher, path, {'status': 'done'}).status_code, 200)
        before = self._get(self.student, '/api/projects/p1/checkins').get_json()['total']
        self.assertEqual(self._patch(self.teacher, path, {'status': 'done'}).status_code, 200)
        self.assertEqual(self._get(self.student, '/api/projects/p1/checkins').get_json()['total'], before)
        self.assert_error(self._delete(self.student, path), 409, 'TASK_IN_PROGRESS')

    def test_concurrent_task_claim(self):
        from concurrent.futures import ThreadPoolExecutor
        task = self.create_task(self.student, 'p1')

        def claim(user):
            with self.app.test_client() as client:
                return client.patch(f"/api/tasks/{task['id']}", json={'assigneeId': user['user']['id']},
                                    headers={'Authorization': f"Bearer {user['token']}"}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(claim, [self.student, self.student2]))
        self.assertEqual(sorted(statuses), [200, 403])

    def test_finish_permissions_snapshot_and_readonly(self):
        project = self.create_project(self.student)
        pid = project['id']
        path = f'/api/projects/{pid}'
        self._post(self.student2, path + '/join')
        self.assert_error(self._patch(self.student2, path, {'status': 'finished'}), 403, 'FORBIDDEN')
        self.assert_error(self._patch(self.student, path, {'status': 'finished'}), 409, 'TASKS_INCOMPLETE')
        task = self.create_task(self.student, pid)
        self.complete_task(task['id'])
        res = self._patch(self.student, path, {'status': 'finished'})
        self.assertEqual(res.status_code, 200, res.get_json())
        archive = self._get(self.student, path + '/archive').get_json()
        self.assertEqual(archive['finishedBy']['id'], 'u1')
        self.assertEqual(archive['assessment']['summary']['done'], 1)
        self.assertFalse(res.get_json()['permissions']['edit'])
        self.assert_error(self._patch(self.student, path, {'status': 'finished'}), 409, 'PROJECT_FINISHED')
        for endpoint, body in (('/tasks', {'title': 'late'}), ('/notes', {'content': 'late'}),
                               ('/mind-nodes', {'label': 'late'}), ('/checkins', {'content': 'late'})):
            self.assert_error(self._post(self.student, path + endpoint, body), 409, 'PROJECT_FINISHED')
        self.assert_error(self._patch(self.teacher, path, {'description': 'late'}), 409, 'PROJECT_FINISHED')
        self.assert_error(self._patch(self.teacher, f"/api/tasks/{task['id']}", {'status': 'done'}), 409, 'PROJECT_FINISHED')
        self.assert_error(self._post(self.student4, path + '/join'), 409, 'PROJECT_FINISHED')
        self._post(self.teacher, path + '/annotations', {'content': '结题后补充意见'})
        self.assertEqual(self._get(self.student, path + '/archive').get_json(), archive)

    def test_review_scope_and_readonly_viewer(self):
        teacher = self.register_teacher('unrelated')
        for path in ('/api/projects/p1/assessment', '/api/projects/p1/tasks'):
            self.assert_error(self._get(teacher, path), 403, 'FORBIDDEN')
        self.assert_error(self._patch(teacher, '/api/tasks/t3', {'status': 'done'}), 403, 'FORBIDDEN')
        self.assert_error(self._post(teacher, '/api/projects/p1/annotations', {'content': 'x'}), 403, 'FORBIDDEN')
        project = self.create_project(self.student)
        viewer = self._get(self.student2, f"/api/projects/{project['id']}").get_json()
        self.assertFalse(viewer['permissions']['edit'])
        self.assertFalse(viewer['permissions']['finish'])

    def test_assessment_evidence_and_empty_project(self):
        from datetime import datetime, timezone
        from app.assessment import assess_project
        project = self.create_project(self.student)
        empty = self._get(self.student, f"/api/projects/{project['id']}/assessment").get_json()
        self.assertIsNone(empty['summary']['completionRate'])
        self.assertIsNone(empty['members'][0]['completionShare'])
        self.assertEqual([r['code'] for r in empty['risks']], ['NO_TASKS'])
        with self.app.app_context():
            execute("UPDATE tasks SET dueDate = '2020-01-01T00:00:00Z' WHERE id = 't4'")
            execute("UPDATE tasks SET updatedAt = '2020-01-01T00:00:00Z', statusChangedAt = '2020-01-01T00:00:00Z' WHERE id = 't3'")
            execute("UPDATE tasks SET dueDate = 'invalid' WHERE id = 't5'")
            commit()
            result = assess_project(query_one("SELECT * FROM projects WHERE id = 'p1'"),
                                    datetime(2026, 9, 11, tzinfo=timezone.utc))
        risks = {r['code']: r for r in result['risks']}
        self.assertIn('t4', risks['OVERDUE']['taskIds'])
        self.assertIn('t3', risks['REVIEW_WAIT']['taskIds'])
        self.assertNotIn('t5', risks['OVERDUE']['taskIds'])
        self.assertTrue(all(0 <= m['activeDays'] <= 7 for m in result['members']))

    def test_auth_rejects_nontext_and_oversized_fields(self):
        for bad in (123456, ['student'], {'name': 'student'}, True, 'x' * 300):
            for field in ('username', 'password'):
                body = {'username': 'student', 'password': '123456', field: bad}
                self.assert_error(self.client.post('/api/sessions', json=body), 400, 'VALIDATION_ERROR')
            for field in ('username', 'password', 'name'):
                body = {'username': 'newstudent', 'password': '123456', 'name': 'Name', 'role': 'student', field: bad}
                self.assert_error(self.client.post('/api/users', json=body), 400, 'VALIDATION_ERROR')

    def test_task_dates_and_validation_rollback(self):
        for value in (['2026-09-15'], {'date': 'today'}, True, 123, 'not-a-date', '2026-02-30', '2026-09-15T10:00:00'):
            res = self._post(self.student, '/api/projects/p1/tasks', {'title': 'date test', 'dueDate': value})
            self.assert_error(res, 400, 'VALIDATION_ERROR')
        task = self._post(self.student, '/api/projects/p1/tasks', {'title': 'date test', 'dueDate': '2026-09-15'}).get_json()
        self.assertEqual(task['dueDate'], '2026-09-15T23:59:59.999999Z')
        path = f"/api/tasks/{task['id']}"
        for body in ({'title': 'changed', 'dueDate': {}}, {'title': ['bad']}, {'description': 'x' * 5001}, {'unknown': True}):
            self.assert_error(self._patch(self.student, path, body), 400, 'VALIDATION_ERROR')
        with self.app.app_context():
            current = query_one('SELECT * FROM tasks WHERE id = ?', (task['id'],))
        # 校验失败必须不落库；逐字段对比时忽略响应里的派生字段（由 join 或聚合得出，不落库）
        derived = {'verifiedByName', 'focusMinutes', 'peerReviewCount'}
        self.assertEqual({k: v for k, v in task.items() if k not in derived},
                         {k: v for k, v in current.items() if k not in derived})
        res = self._patch(self.student, path, {'dueDate': '2026-09-15T23:59:59+08:00'})
        self.assertEqual(res.get_json()['dueDate'], '2026-09-15T15:59:59.000000Z')

    def test_review_clock_survives_edits_and_repeated_status(self):
        task = self.create_task(self.student, 'p1')
        path = f"/api/tasks/{task['id']}"
        self._patch(self.student, path, {'assigneeId': 'u1', 'status': 'doing'})
        with self.app.app_context():
            execute("UPDATE tasks SET statusChangedAt = '2020-01-01T00:00:00Z' WHERE id = ?", (task['id'],))
            commit()
        # 进行中编辑内容不重置状态计时，否则「验收等待过久」会被一次改字洗掉
        edited = self._patch(self.student, path, {'description': 'Clarification'}).get_json()
        self.assertEqual(edited['statusChangedAt'], '2020-01-01T00:00:00Z')
        self.assertEqual(edited['description'], 'Clarification')
        self.assertEqual(self._patch(self.teacher, path, {'status': 'review'}).status_code, 409)

        self._patch(self.student, path, {'status': 'review'})
        # 待验收期间内容冻结：教师验收的对象必须等于提交物
        for body in ({'title': '验收前改标题'}, {'description': '验收前改描述'}, {'dueDate': '2026-09-15'}):
            self.assert_error(self._patch(self.student, path, body), 409, 'TASK_IN_REVIEW')
        # 撤回是唯一的修改入口，撤回后可正常编辑并重新提交
        self.assertEqual(self._patch(self.student, path, {'status': 'doing'}).status_code, 200)
        self.assertEqual(self._patch(self.student, path, {'title': '撤回后修改'}).status_code, 200)
        self._patch(self.student, path, {'status': 'review'})

        with self.app.app_context():
            execute("UPDATE tasks SET statusChangedAt = '2020-01-01T00:00:00Z' WHERE id = ?", (task['id'],))
            commit()
        current = next(t for t in self._get(self.student, '/api/projects/p1/tasks').get_json()['items']
                       if t['id'] == task['id'])
        before = self._get(self.student, '/api/projects/p1/task-logs').get_json()['total']
        for user in (self.student, self.teacher):
            repeated = self._patch(user, path, {'status': 'review'})
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(repeated.get_json(), current)
        self.assertEqual(self._get(self.student, '/api/projects/p1/task-logs').get_json()['total'], before)
        assessment = self._get(self.student, '/api/projects/p1/assessment').get_json()
        risk = next(r for r in assessment['risks'] if r['code'] == 'REVIEW_WAIT')
        self.assertIn(task['id'], risk['taskIds'])
        returned = self._patch(self.teacher, path, {'status': 'doing'}).get_json()
        self.assertGreater(returned['statusChangedAt'], current['statusChangedAt'])

    def test_task_status_clock_migration_preserves_existing_data(self):
        with self.app.app_context():
            before = query_one("SELECT id, title, updatedAt FROM tasks WHERE id = 't3'")
            execute('ALTER TABLE tasks DROP COLUMN statusChangedAt')
            commit()
        migrated = create_app({'DATABASE': self.db_path})
        with migrated.app_context():
            row = query_one("SELECT * FROM tasks WHERE id = 't3'")
            self.assertEqual(row['statusChangedAt'], before['updatedAt'])
            self.assertEqual(row['title'], before['title'])

    # ------------------------------------------------------------ 批注作者

    def test_annotation_exposes_real_author(self):
        """批注必须带上作者姓名，否则前端只能猜（曾经一律显示同一位老师）。"""
        items = self._get(self.student, '/api/projects/p1/annotations').get_json()['items']
        self.assertEqual(items, [])

        t2 = self.register_teacher()
        self._post(self.teacher, '/api/groups/g1/members', {'userId': t2['user']['id'], 'role': 'teacher'})
        res = self._post(t2, '/api/projects/p1/annotations', {'content': '第二位教师的批注'})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()['userName'], '李老师')

        items = self._get(self.student, '/api/projects/p1/annotations').get_json()['items']
        self.assertEqual([a['userId'] for a in items], [t2['user']['id']])
        self.assertEqual([a['userName'] for a in items], ['李老师'])

        # 结题档案里的批注同样带作者（p2 为已结题的种子项目）
        archive = self._get(self.student, '/api/projects/p2/archive').get_json()
        self.assertTrue(archive['annotations'])
        for annotation in archive['annotations']:
            self.assertEqual(annotation['userName'], '王老师')

    # ------------------------------------------------------------ 用户组删除

    def test_group_delete_blocked_while_projects_attached(self):
        """删除仍挂着项目的分组会让负责教师永久失去验收能力，必须拒绝。"""
        for user in (self.teacher, self.schooladmin):
            self.assert_error(self._delete(user, '/api/groups/g1'), 409, 'GROUP_HAS_PROJECTS')
        self.assertEqual(self._get(self.teacher, '/api/groups/g1/members').status_code, 200)

        gid = self._post(self.teacher, '/api/groups', {'name': '空组'}).get_json()['id']
        self._post(self.teacher, f'/api/groups/{gid}/invites', {'userId': 'u4'})
        self.assertEqual(self._delete(self.teacher, f'/api/groups/{gid}').status_code, 204)
        with self.app.app_context():
            self.assertIsNone(query_one('SELECT 1 FROM group_invites WHERE groupId = ?', (gid,)))
        self.assert_error(self._get(self.teacher, f'/api/groups/{gid}/members'), 404, 'GROUP_NOT_FOUND')

    # ------------------------------------------------------------ 会话有效期

    def test_session_expiry_and_password_reset_revocation(self):
        """token 有有效期；重置口令同时吊销该用户已签发的会话。"""
        with self.app.app_context():
            execute("UPDATE sessions SET expiresAt = '2000-01-01T00:00:00.000000Z' WHERE token = ?",
                    (self.student['token'],))
            commit()
        self.assert_error(self._get(self.student, '/api/me'), 401, 'UNAUTHORIZED')

        fresh = self._login('student', '123456')
        self.assertEqual(self._get(fresh, '/api/me').status_code, 200)
        res = self._patch(self.schooladmin, '/api/admin/users/u1', {'password': 'newpass123'})
        self.assertEqual(res.status_code, 200)
        self.assert_error(self._get(fresh, '/api/me'), 401, 'UNAUTHORIZED')
        self.assertEqual(
            self.client.post('/api/sessions', json={'username': 'student', 'password': '123456'}).status_code, 401)
        self.assertEqual(
            self.client.post('/api/sessions', json={'username': 'student', 'password': 'newpass123'}).status_code, 201)

    # ------------------------------------------------------------ 移出分组 / 时区 / 搜索

    def test_group_member_removal_revokes_project_access(self):
        """移出分组必须同时收回该组项目权限，否则组间隔离形同虚设。"""
        self.assertEqual(self._get(self.student2, '/api/projects/p1').status_code, 200)
        self.assertEqual(
            self._post(self.student2, '/api/projects/p1/tasks', {'title': '移出前可建'}).status_code, 201)
        self.assertEqual(self._delete(self.teacher, '/api/groups/g1/members/u2').status_code, 204)

        self.assert_error(self._get(self.student2, '/api/projects/p1'), 403, 'FORBIDDEN')
        self.assert_error(self._get(self.student2, '/api/projects/p1/tasks'), 403, 'FORBIDDEN')
        self.assert_error(self._post(self.student2, '/api/projects/p1/tasks', {'title': '移出后不可建'}),
                          403, 'FORBIDDEN')
        self.assert_error(self._post(self.student2, '/api/projects/p1/checkins', {'content': 'x'}),
                          403, 'FORBIDDEN')
        # 组内其他成员不受影响
        self.assertEqual(self._get(self.student, '/api/projects/p1').status_code, 200)
        # 重新入组后可以读项目（同组成员可读），但要重新加入项目才能再编辑
        self._post(self.teacher, '/api/groups/g1/members', {'userId': 'u2', 'role': 'member'})
        self.assertEqual(self._get(self.student2, '/api/projects/p1').status_code, 200)
        self.assert_error(self._post(self.student2, '/api/projects/p1/tasks', {'title': '未重新加入'}),
                          403, 'FORBIDDEN')
        self.assertEqual(self._post(self.student2, '/api/projects/p1/join').status_code, 201)
        self.assertEqual(self._post(self.student2, '/api/projects/p1/tasks', {'title': '重新加入后可建'}).status_code, 201)

    def test_focus_stats_respects_client_timezone(self):
        """自然日按客户端时区划分：否则本地早上 8 点才翻页，「今日专注」与直觉不符。"""
        from datetime import datetime, timedelta, timezone

        # 用全新账号，避免种子里的历史专注记录干扰边界断言
        created = self._post(self.schooladmin, '/api/admin/users',
                             {'username': 'tzprobe', 'password': '123456', 'name': '时区探针', 'role': 'student'})
        self.assertEqual(created.status_code, 201, created.get_json())
        probe = self._login('tzprobe', '123456')
        # 一条记录：UTC 昨天中午，在 UTC 和 UTC+8 都落在昨天
        boundary = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0)
        with self.app.app_context():
            execute('INSERT INTO focus_sessions (id, userId, durationMin, type, createdAt) VALUES (?, ?, ?, ?, ?)',
                    ('fs_boundary', probe['user']['id'], 30, 'focus', iso(boundary)))
            commit()

        utc8_tz = timezone(timedelta(minutes=480))
        # 用 week 数组验证：记录在 UTC 和 UTC+8 下出现在各自的日期槽位
        utc = self._get(probe, '/api/focus/stats?days=3&tzOffset=0').get_json()
        boundary_utc = boundary.date().isoformat()
        utc_entry = next(e for e in utc['week'] if e['date'] == boundary_utc)
        self.assertEqual(utc_entry['minutes'], 30)

        local = self._get(probe, '/api/focus/stats?days=3&tzOffset=480').get_json()
        boundary_local = boundary.astimezone(utc8_tz).date().isoformat()
        local_entry = next(e for e in local['week'] if e['date'] == boundary_local)
        self.assertEqual(local_entry['minutes'], 30)
        # UTC 和 UTC+8 下的日期不同，证明时区偏移生效
        self.assertNotEqual(utc_entry['date'], local_entry['date'])

        # 越界或非法偏移量必须被夹紧而不是报错
        self.assertEqual(self._get(probe, '/api/focus/stats?tzOffset=abc').status_code, 200)
        capped = self._get(probe, '/api/focus/stats?days=3&tzOffset=840').get_json()
        self.assertEqual(self._get(probe, '/api/focus/stats?days=3&tzOffset=99999').get_json(), capped)

    def test_user_search_hides_higher_tiers(self):
        """普通教师不应通过成员搜索看到管理员账号。"""
        roles = {u['role'] for u in self._get(self.teacher, '/api/users').get_json()['items']}
        self.assertTrue(roles <= {'student', 'teacher'}, f'教师可见了过高层级：{roles}')
        self.assertIn('student', roles)
        # 管理角色可以搜索到同级及以下（便于加人），但看不到更高层级
        scopes = {u['role'] for u in self._get(self.schooladmin, '/api/users').get_json()['items']}
        self.assertIn('schooladmin', scopes)
        self.assertNotIn('admin', scopes)
        self.assertNotIn('superadmin', scopes)
        # 关键词搜索同样受层级约束
        found = self._get(self.teacher, '/api/users?keyword=admin').get_json()['items']
        self.assertEqual([u for u in found if u['role'] in ('admin', 'superadmin', 'schooladmin')], [])

    def test_team_limit_comes_from_config(self):
        """队伍上限读配置，改配置即生效（原先两处硬编码 4）。"""
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            app = create_app({'DATABASE': path, 'TEAM_LIMIT': 2,
                              'PASSWORD_HASH_METHOD': 'pbkdf2:sha256:1000'})
            client = app.test_client()

            def auth_for(username):
                data = client.post('/api/sessions', json={'username': username, 'password': '123456'}).get_json()
                return {'Authorization': f"Bearer {data['token']}"}

            # u1/u2/u3 同属 g1，可加入同一个组内项目
            created = client.post('/api/projects', headers=auth_for('student'), json={'topicId': 'topic1'})
            project_id = created.get_json()['id']
            self.assertEqual(
                client.post(f'/api/projects/{project_id}/join', headers=auth_for('student2')).status_code, 201)
            res = client.post(f'/api/projects/{project_id}/join', headers=auth_for('student3'))
            self.assertEqual(res.status_code, 409)
            self.assertEqual(res.get_json()['error']['message'], '队伍已满（最多 2 人）')
        finally:
            os.unlink(path)

    # ------------------------------------------------------------ 批注转任务 / 预警回应

    def test_annotation_to_task_closure(self):
        """批注 → 任务 → 验收 → 档案：一条可追溯的链路。"""
        pid = self.create_project(self.student, name='批注闭环')['id']
        created_annotation = self._post(self.teacher, f'/api/projects/{pid}/annotations',
                                        {'content': '补充量化结论'})
        self.assertEqual(created_annotation.status_code, 201)
        aid = created_annotation.get_json()['id']

        # 只有负责教师能转任务
        self.assert_error(self._post(self.student, f'/api/annotations/{aid}/tasks', {}), 403, 'FORBIDDEN')
        other_teacher = self.register_teacher()
        self.assert_error(self._post(other_teacher, f'/api/annotations/{aid}/tasks', {}), 403, 'FORBIDDEN')
        self.assert_error(self._post(self.teacher, '/api/annotations/nope/tasks', {}), 404, 'NOT_FOUND')
        self.assert_error(self._post(self.teacher, f'/api/annotations/{aid}/tasks', {'assigneeId': 'u4'}),
                          400, 'VALIDATION_ERROR')

        created = self._post(self.teacher, f'/api/annotations/{aid}/tasks', {'assigneeId': 'u1'})
        self.assertEqual(created.status_code, 201, created.get_json())
        task = created.get_json()
        self.assertEqual(task['sourceAnnotationId'], aid)
        self.assertEqual(task['title'], '补充量化结论')  # 默认沿用批注内容
        self.assertEqual((task['assigneeId'], task['status']), ('u1', 'todo'))

        # 学生侧能看到批注派生出的任务，以及是谁提的
        items = self._get(self.student, f'/api/projects/{pid}/annotations').get_json()['items']
        self.assertEqual([t['id'] for t in items[0]['linkedTasks']], [task['id']])
        self.assertEqual(items[0]['userName'], '王老师')
        logs = self._get(self.student, f'/api/projects/{pid}/task-logs').get_json()['items']
        self.assertIn('由教师批注创建任务', [row['detail'] for row in logs])

        # 走完「认领 → 验收」后结题，档案里保留批注与派生任务的最终状态
        self.complete_task(task['id'], self.student)
        self.assertEqual(self._patch(self.student, f'/api/projects/{pid}', {'status': 'finished'}).status_code, 200)
        archive = self._get(self.student, f'/api/projects/{pid}/archive').get_json()
        self.assertEqual(archive['annotations'][0]['userName'], '王老师')
        self.assertEqual(archive['annotations'][0]['linkedTasks'][0]['status'], 'done')

    def test_risk_response_dialogue(self):
        """成员可以回应预警，记录下的是「谁在何时说了什么」。"""
        self.assert_error(self._post(self.student, '/api/projects/p1/risk-responses',
                                     {'riskCode': 'NOPE', 'content': 'x'}), 400, 'VALIDATION_ERROR')
        self.assert_error(self._post(self.student, '/api/projects/p1/risk-responses',
                                     {'riskCode': 'UNASSIGNED', 'content': '   '}), 400, 'VALIDATION_ERROR')
        # 教师不是项目成员，不能以成员身份回应（教师走批注通道）
        self.assert_error(self._post(self.teacher, '/api/projects/p1/risk-responses',
                                     {'riskCode': 'UNASSIGNED', 'content': 'x'}), 403, 'FORBIDDEN')

        res = self._post(self.student, '/api/projects/p1/risk-responses',
                         {'riskCode': 'UNASSIGNED', 'content': '线下已完成，证据稍后补录'})
        self.assertEqual(res.status_code, 201, res.get_json())
        self.assertEqual(res.get_json()['userName'], '张三')

        assessment = self._get(self.teacher, '/api/projects/p1/assessment').get_json()
        risk = next(r for r in assessment['risks'] if r['code'] == 'UNASSIGNED')
        self.assertEqual([r['content'] for r in risk['responses']], ['线下已完成，证据稍后补录'])
        self.assertEqual(risk['responses'][0]['userName'], '张三')
        # 只有带说明的预警才带 responses 字段内容，其他预警为空数组
        for other in assessment['risks']:
            self.assertIsInstance(other['responses'], list)

        self.assert_error(self._post(self.student, '/api/projects/p1/risk-responses',
                                     {'riskCode': 'UNASSIGNED', 'content': 'x' * 501}), 400, 'VALIDATION_ERROR')
        # 已结题项目的过程记录只读
        self.assert_error(self._post(self.student, '/api/projects/p2/risk-responses',
                                     {'riskCode': 'UNASSIGNED', 'content': 'x'}), 409, 'PROJECT_FINISHED')

    # ------------------------------------------------------------ 前端托管

    def test_frontend_serving(self):
        """后端在有构建产物时一并提供前端页面，并为前端路由回退 index.html。"""
        dist = tempfile.mkdtemp()
        try:
            with open(os.path.join(dist, 'index.html'), 'w', encoding='utf-8') as fh:
                fh.write('<!doctype html><title>InnoArk</title>')
            os.makedirs(os.path.join(dist, 'assets'), exist_ok=True)
            with open(os.path.join(dist, 'assets', 'app.js'), 'w', encoding='utf-8') as fh:
                fh.write('console.log(1)')
            with open(os.path.join(dist, 'favicon.svg'), 'w', encoding='utf-8') as fh:
                fh.write('<svg/>')
            client = create_app({'DATABASE': self.db_path, 'FRONTEND_DIST': dist,
                                 'PASSWORD_HASH_METHOD': 'pbkdf2:sha256:1000'}).test_client()

            def fetch(path):
                """取回响应并关闭文件句柄，避免测试输出里堆满 ResourceWarning。"""
                res = client.get(path)
                res.get_data()
                res.close()
                return res

            root = fetch('/')
            self.assertEqual(root.status_code, 200)
            self.assertIn('text/html', root.headers['Content-Type'])
            self.assertIn(b'InnoArk', root.data)
            # history 路由的深链接回退到 index.html
            for path in ('/project/p1', '/resources'):
                res = fetch(path)
                self.assertEqual(res.status_code, 200, path)
                self.assertIn(b'InnoArk', res.data, path)
            # 真实静态资源正常返回
            self.assertEqual(fetch('/assets/app.js').data, b'console.log(1)')
            self.assertEqual(fetch('/favicon.svg').data, b'<svg/>')
            # 浏览器惯例请求的 favicon.ico 转发到页面声明的 svg
            self.assertEqual(fetch('/favicon.ico').status_code, 302)
            # 带扩展名却不存在：404 且必须是 JSON，不能回退成 HTML
            missing = fetch('/assets/missing.js')
            self.assertEqual(missing.status_code, 404)
            self.assertIn('application/json', missing.headers['Content-Type'])
            self.assertEqual(missing.get_json()['error']['code'], 'NOT_FOUND')
            # 接口路径不受影响
            self.assert_error(fetch('/api/nope'), 404, 'NOT_FOUND')
            self.assert_error(fetch('/api/me'), 401, 'UNAUTHORIZED')
        finally:
            shutil.rmtree(dist, ignore_errors=True)

    def test_frontend_root_without_build(self):
        """没有构建产物时根路径给出可操作提示，而不是让人只看到接口 404。"""
        empty = tempfile.mkdtemp()
        try:
            client = create_app({'DATABASE': self.db_path, 'FRONTEND_DIST': empty,
                                 'PASSWORD_HASH_METHOD': 'pbkdf2:sha256:1000'}).test_client()
            res = client.get('/')
            self.assertEqual(res.status_code, 200)
            self.assertIn('yarn build', res.get_json()['howto'])
            # 未构建时不接管路径，未知路径保持 JSON 404
            self.assert_error(client.get('/project/p1'), 404, 'NOT_FOUND')
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    # ------------------------------------------------------------ 验收标准与回执

    def test_task_criteria_and_verification_receipt(self):
        """验收标准逐条核对；验收人与时间留痕，批注线程据此给出回执。"""
        pid = self.create_project(self.student, name='标准验收')['id']
        aid = self._post(self.teacher, f'/api/projects/{pid}/annotations',
                         {'content': '补充量化结论'}).get_json()['id']
        task = self._post(self.teacher, f'/api/annotations/{aid}/tasks', {
            'title': '改一版', 'criteria': ['有量化数据', '有误差范围'],
        }).get_json()
        self.assertEqual([c['text'] for c in json.loads(task['criteria'])], ['有量化数据', '有误差范围'])

        # 验收标准有上限与格式约束
        for bad in ({'criteria': 'not-a-list'}, {'criteria': ['']},
                    {'criteria': ['x'] * 11}, {'criteria': [{'text': 'y' * 201}]}):
            self.assert_error(self._patch(self.student, f"/api/tasks/{task['id']}", bad),
                              400, 'VALIDATION_ERROR')

        for body in ({'assigneeId': 'u1'}, {'status': 'doing'}, {'status': 'review'}):
            self.assertEqual(self._patch(self.student, f"/api/tasks/{task['id']}", body).status_code, 200)
        # 待验收期间标准同样冻结
        self.assert_error(self._patch(self.student, f"/api/tasks/{task['id']}", {'criteria': ['改了']}),
                          409, 'TASK_IN_REVIEW')

        verified = self._patch(self.teacher, f"/api/tasks/{task['id']}", {
            'status': 'done',
            'criteria': [{'text': '有量化数据', 'done': True}, {'text': '有误差范围', 'done': False}],
        }).get_json()
        self.assertEqual(verified['verifiedBy'], 't1')
        self.assertIsNotNone(verified['verifiedAt'])

        # 任务动态记录达标情况，而不是只说"已完成"
        logs = [row['detail'] for row in self._get(self.student, f'/api/projects/{pid}/task-logs')
                .get_json()['items']]
        self.assertIn('状态更新为 已完成（验收标准 1/2 项达标）', logs)

        # 批注线程显示验收回执
        linked = self._get(self.student, f'/api/projects/{pid}/annotations').get_json()['items'][0]['linkedTasks'][0]
        self.assertEqual(linked['status'], 'done')
        self.assertEqual(linked['verifiedByName'], '王老师')
        self.assertIsNotNone(linked['verifiedAt'])

        # 结题档案里同样保留验收信息
        self.assertEqual(self._patch(self.student, f'/api/projects/{pid}', {'status': 'finished'}).status_code, 200)
        archive = self._get(self.student, f'/api/projects/{pid}/archive').get_json()
        self.assertEqual(archive['tasks'][0]['verifiedBy'], 't1')

    def test_batch_verify(self):
        """批量验收逐项返回结果：状态已变的跳过，其余仍生效。"""
        pid = self.create_project(self.student, name='批量验收')['id']
        ids = []
        for i in range(3):
            task = self.create_task(self.student, pid, title=f'批量{i}')
            for body in ({'assigneeId': 'u1'}, {'status': 'doing'}, {'status': 'review'}):
                self._patch(self.student, f"/api/tasks/{task['id']}", body)
            ids.append(task['id'])
        # 一项仍处于进行中：批量时只跳过它
        draft = self.create_task(self.student, pid, title='未提交')
        self._patch(self.student, f"/api/tasks/{draft['id']}", {'assigneeId': 'u1'})

        self.assert_error(self._post(self.student, f'/api/projects/{pid}/tasks/batch-verify', {'taskIds': ids}),
                          403, 'FORBIDDEN')
        self.assert_error(self._post(self.teacher, f'/api/projects/{pid}/tasks/batch-verify', {'taskIds': []}),
                          400, 'VALIDATION_ERROR')
        self.assert_error(self._post(self.teacher, f'/api/projects/{pid}/tasks/batch-verify',
                                     {'taskIds': ['x'] * 51}), 400, 'VALIDATION_ERROR')

        res = self._post(self.teacher, f'/api/projects/{pid}/tasks/batch-verify',
                         {'taskIds': [*ids, draft['id'], 'ghost']})
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertEqual(body['verified'], 3)
        by_id = {r['id']: r['status'] for r in body['results']}
        self.assertEqual(by_id[draft['id']], 'skipped')
        self.assertEqual(by_id['ghost'], 'not_found')
        for task_id in ids:
            current = next(t for t in self._get(self.student, f'/api/projects/{pid}/tasks').get_json()['items']
                           if t['id'] == task_id)
            self.assertEqual(current['status'], 'done')
            self.assertEqual(current['verifiedBy'], 't1')

    # ------------------------------------------------------------ 预警证据与幂等打卡

    def test_risk_response_with_evidence(self):
        """补充说明可以引用本项目内的打卡或任务作为证据。"""
        self._post(self.student, '/api/projects/p1/checkins', {'content': '补了一版数据'})
        task = self.create_task(self.student, 'p1', title='可引用的任务')

        self.assert_error(self._post(self.student, '/api/projects/p1/risk-responses', {
            'riskCode': 'UNASSIGNED', 'content': 'x',
            'evidenceType': 'checkin', 'evidenceId': task['id'],
        }), 400, 'VALIDATION_ERROR')  # 类型与 id 不匹配
        self.assert_error(self._post(self.student, '/api/projects/p1/risk-responses', {
            'riskCode': 'UNASSIGNED', 'content': 'x',
            'evidenceType': 'task', 'evidenceId': 'not-in-project',
        }), 400, 'VALIDATION_ERROR')
        self.assert_error(self._post(self.student, '/api/projects/p1/risk-responses', {
            'riskCode': 'UNASSIGNED', 'content': 'x', 'evidenceType': 'nope', 'evidenceId': task['id'],
        }), 400, 'VALIDATION_ERROR')

        res = self._post(self.student, '/api/projects/p1/risk-responses', {
            'riskCode': 'UNASSIGNED', 'content': '已完成', 'evidenceType': 'task', 'evidenceId': task['id'],
        })
        self.assertEqual(res.status_code, 201, res.get_json())
        assessment = self._get(self.teacher, '/api/projects/p1/assessment').get_json()
        risk = next(r for r in assessment['risks'] if r['code'] == 'UNASSIGNED')
        entry = next(r for r in risk['responses'] if r['content'] == '已完成')
        self.assertEqual(entry['evidenceType'], 'task')
        self.assertEqual(entry['evidenceLabel'], '可引用的任务')

        # 证据被删除后说明仍保留，只是标签为空
        self._delete(self.student, f"/api/tasks/{task['id']}")
        assessment = self._get(self.student, '/api/projects/p1/assessment').get_json()
        entry = next(r for r in next(r for r in assessment['risks'] if r['code'] == 'UNASSIGNED')['responses']
                     if r['content'] == '已完成')
        self.assertIsNone(entry['evidenceLabel'])

    def test_checkin_idempotency_and_revision(self):
        """离线补交用 clientId 去重；修订号随写入递增，供前端判断是否重新拉取。"""
        before = self._get(self.student, '/api/projects/p1/revision').get_json()['revision']
        first = self._post(self.student, '/api/projects/p1/checkins',
                           {'content': '离线补交', 'clientId': 'c-abc'})
        self.assertEqual(first.status_code, 201)
        # 断网重试：返回已入库的那条而不是再记一次
        again = self._post(self.student, '/api/projects/p1/checkins',
                           {'content': '离线补交', 'clientId': 'c-abc'})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.get_json()['id'], first.get_json()['id'])
        mine = [c for c in self._get(self.student, '/api/projects/p1/checkins').get_json()['items']
                if c.get('clientId') == 'c-abc']
        self.assertEqual(len(mine), 1)
        # 不同用户的相同 clientId 互不影响
        other = self._post(self.student2, '/api/projects/p1/checkins',
                           {'content': '另一个人', 'clientId': 'c-abc'})
        self.assertEqual(other.status_code, 201)

        after = self._get(self.student, '/api/projects/p1/revision').get_json()['revision']
        self.assertGreater(after, before)
        # 修订号需要项目读取权限
        self.assert_error(self._get(self.student4, '/api/projects/p1/revision'), 403, 'FORBIDDEN')

    def test_assessment_stream_and_trend(self):
        """SSE 只推变更信号并会到点结束；趋势在没有历史快照时为 null。"""
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            app = create_app({'DATABASE': path, 'PASSWORD_HASH_METHOD': 'pbkdf2:sha256:1000',
                              'STREAM_INTERVAL_SECONDS': 0.01, 'STREAM_MAX_SECONDS': 0.05})
            client = app.test_client()
            token = client.post('/api/sessions', json={'username': 'student', 'password': '123456'}).get_json()
            auth = {'Authorization': f"Bearer {token['token']}"}
            res = client.get('/api/projects/p1/stream', headers=auth)
            self.assertEqual(res.status_code, 200)
            self.assertIn('text/event-stream', res.headers['Content-Type'])
            payload = res.get_data(as_text=True)
            self.assertIn('event: revision', payload)
            self.assertIn('data: {"revision":', payload)
            self.assertIn('event: reconnect', payload)
            # 未登录不可订阅
            self.assertEqual(client.get('/api/projects/p1/stream').status_code, 401)
        finally:
            os.unlink(path)

        # 首次查看没有历史快照：不编造趋势
        self.assertIsNone(self._get(self.student, '/api/projects/p1/assessment').get_json()['trend'])
        # 注入昨天与前天的快照后能给出对比
        with self.app.app_context():
            for day, done in (('2026-09-09', 0), ('2026-09-10', 0)):
                execute('INSERT INTO assessment_snapshots (id, projectId, snapshotDate, content, createdAt) '
                        'VALUES (?, ?, ?, ?, ?)',
                        (f'as-{day}', 'p1', day,
                         json.dumps({'summary': {'total': 1, 'done': done, 'overdue': 0, 'review': 0,
                                                 'unassigned': 0, 'activeMembers': 0},
                                     'risks': [{'code': 'NO_TASKS'}], 'members': [], 'tasks': [],
                                     'version': 'process-v1', 'asOf': '', 'status': 'active'}),
                         f'{day}T00:00:00Z'))
            commit()
        trend = self._get(self.student, '/api/projects/p1/assessment').get_json()['trend']
        self.assertEqual(trend['baseDate'], '2026-09-10')  # 取最近一份历史快照
        self.assertGreater(trend['metrics']['total']['now'], trend['metrics']['total']['before'])
        self.assertIn('NO_TASKS', trend['risksResolved'])

    # ------------------------------------------------------------ 错题本与间隔复习

    def test_wrong_answer_book_and_spaced_repetition(self):
        """错题入服务端错题本，按 3/7/14 天推进；答对不泄露答案，答错退回第一轮。"""
        attempt = self.play_round(self.student, correct=0, count=3)
        self.assertEqual(attempt.status_code, 201, attempt.get_json())
        book = self._get(self.student, '/api/quiz/wrong-answers').get_json()['items']
        self.assertEqual(len(book), 3)
        for entry in book:
            self.assertNotIn('answer', entry)  # 错题本不返回答案
            self.assertNotIn('explanation', entry)
            self.assertEqual(entry['stage'], 0)
        self.assertEqual(self._get(self.student, '/api/quiz/stats').get_json()['wrong']['open'], 3)
        # 刚答错的题 3 天后才到期
        self.assertEqual(len(self._get(self.student, '/api/quiz/wrong-answers?due=1').get_json()['items']), 0)

        question_id = book[0]['questionId']
        with self.app.app_context():
            answer = query_one('SELECT answer FROM quiz_questions WHERE id = ?', (question_id,))['answer']
        wrong_choice = (answer + 1) % len(book[0]['options'])
        for expected_stage in (1, 2, 3):
            res = self._post(self.student, f'/api/quiz/wrong-answers/{question_id}/review',
                             {'choice': answer})
            self.assertEqual(res.status_code, 200, res.get_json())
            self.assertEqual(res.get_json()['stage'], expected_stage)
            self.assertEqual(res.get_json()['resolved'], expected_stage == 3)
        # 已掌握后答错会退回第一轮
        back = self._post(self.student, f'/api/quiz/wrong-answers/{question_id}/review',
                          {'choice': wrong_choice}).get_json()
        self.assertEqual(back['stage'], 0)
        self.assertFalse(back['resolved'])
        self.assert_error(self._post(self.student, f'/api/quiz/wrong-answers/{question_id}/review',
                                     {'choice': 99}), 400, 'VALIDATION_ERROR')
        # 不在错题本里的题：从公共题库里取一道本轮没被抽到的，避免依赖随机抽样结果
        # （曾经写死 q1，而 q1 有约 3/20 的概率被抽中，导致测试偶发失败）
        with self.app.app_context():
            sampled = {entry['questionId'] for entry in book}
            candidates = [row['id'] for row in query_all(
                'SELECT id FROM quiz_questions WHERE groupId IS NULL') if row['id'] not in sampled]
        self.assertTrue(candidates, '应有未被抽到的公共题目')
        self.assert_error(self._post(self.student, f'/api/quiz/wrong-answers/{candidates[0]}/review',
                                     {'choice': 0}), 404, 'NOT_FOUND')
        self.assert_error(self._post(self.student, '/api/quiz/wrong-answers/no-such-question/review',
                                     {'choice': 0}), 404, 'NOT_FOUND')

    # ------------------------------------------------------------ 审计 / 学校 / 重置

    def test_audit_log_records_management_actions(self):
        """改角色、重置口令、删除账号等留下审计；普通教师看不到。"""
        created = self._post(self.schooladmin, '/api/admin/users', {
            'username': 'audit1', 'password': '123456', 'name': '审计对象', 'role': 'teacher',
        }).get_json()
        self._patch(self.schooladmin, f"/api/admin/users/{created['id']}", {'role': 'student'})
        self._patch(self.schooladmin, f"/api/admin/users/{created['id']}", {'password': 'newpass123'})
        self._patch(self.schooladmin, f"/api/admin/users/{created['id']}", {'name': '改名后'})
        self._delete(self.schooladmin, f"/api/admin/users/{created['id']}")

        actions = [row['action'] for row in
                   self._get(self.schooladmin, '/api/admin/audit-logs').get_json()['items']]
        for expected in ('user.create', 'user.role_change', 'user.reset_password', 'user.rename', 'user.delete'):
            self.assertIn(expected, actions)
        # 相同角色不会产生噪音记录
        self._patch(self.schooladmin, '/api/admin/users/u2', {'role': 'student'})
        roles = [row for row in self._get(self.schooladmin, '/api/admin/audit-logs').get_json()['items']
                 if row['action'] == 'user.role_change' and row['targetId'] == 'u2']
        self.assertEqual(roles, [])
        self.assert_error(self._get(self.teacher, '/api/admin/audit-logs'), 403, 'FORBIDDEN')
        # 审计不记录口令内容
        for row in self._get(self.schooladmin, '/api/admin/audit-logs').get_json()['items']:
            self.assertNotIn('newpass123', row['detail'])

    def test_school_scoping(self):
        """校管理员只管理本校分组与账号；未归属数据保持可见以免迁移后失权。"""
        schools = self._get(self.schooladmin, '/api/admin/schools').get_json()['items']
        self.assertTrue(schools)
        school_id = schools[0]['id']
        self.assertEqual(self._get(self.schooladmin, '/api/admin/schools').status_code, 200)

        # 自己的学校可以管理
        mine = self._get(self.schooladmin, '/api/groups').get_json()['items']
        self.assertIn('g1', [g['id'] for g in mine])

        # 造一个别的学校的分组：校管理员看不到也管不了，平台管理员可以
        self._post(self.admin, '/api/groups', {'name': '他校分组'})
        other = [g for g in self._get(self.admin, '/api/groups').get_json()['items']
                 if g['name'] == '他校分组'][0]
        self.assertEqual(self._patch(self.admin, f"/api/groups/{other['id']}", {'schoolId': 'ghost'}).status_code, 400)
        with self.app.app_context():
            execute('INSERT INTO schools (id, name, createdAt) VALUES (?, ?, ?)', ('sch2', '另一所学校', '2026-01-01'))
            execute('UPDATE groups SET schoolId = ? WHERE id = ?', ('sch2', other['id']))
            commit()
        visible_ids = [g['id'] for g in self._get(self.schooladmin, '/api/groups').get_json()['items']]
        self.assertNotIn(other['id'], visible_ids)
        self.assert_error(self._patch(self.schooladmin, f"/api/groups/{other['id']}", {'name': 'x'}),
                          403, 'FORBIDDEN')
        # 归属调整只允许平台管理员
        self.assert_error(self._patch(self.schooladmin, '/api/groups/g1', {'schoolId': school_id}),
                          403, 'FORBIDDEN')
        self.assertEqual(self._patch(self.admin, '/api/groups/g1', {'schoolId': school_id}).status_code, 200)
        # 平台管理员不受学校限制
        self.assertIn(other['id'], [g['id'] for g in self._get(self.admin, '/api/groups').get_json()['items']])

    def test_demo_reset(self):
        """重置恢复初始演示数据；普通教师与校管理员不能触发。"""
        self.create_task(self.student, 'p1', title='重置前创建的任务')
        self.assertEqual(self._post(self.student, '/api/admin/demo/reset').status_code, 403)
        self.assertEqual(self._post(self.teacher, '/api/admin/demo/reset').status_code, 403)
        self.assertEqual(self._post(self.schooladmin, '/api/admin/demo/reset').status_code, 403)
        res = self._post(self.admin, '/api/admin/demo/reset')
        self.assertEqual(res.status_code, 200, res.get_json())

        # 重置清空了会话，旧 token 失效
        self.assert_error(self._get(self.student, '/api/me'), 401, 'UNAUTHORIZED')
        # 种子数据回到初始状态
        fresh = self._login('student', '123456')
        task_titles = [t['title'] for t in self._get(fresh, '/api/projects/p1/tasks').get_json()['items']]
        self.assertNotIn('重置前创建的任务', task_titles)
        self.assertEqual(len(task_titles), 6)
        # 可以反复重置
        again = self._post(self._login('admin', '123456'), '/api/admin/demo/reset')
        self.assertEqual(again.status_code, 200, again.get_json())

    def test_pagination_pushed_to_sql(self):
        """列表分页在数据库完成：过滤与总数一致，越界页返回空。"""
        pid = self.create_project(self.student, name='分页')['id']
        for i in range(5):
            task = self.create_task(self.student, pid, title=f'分页任务{i}')
            if i < 2:
                self._patch(self.student, f"/api/tasks/{task['id']}", {'assigneeId': 'u1'})
        first = self._get(self.student, f'/api/projects/{pid}/tasks?pageSize=2&page=1').get_json()
        self.assertEqual((first['total'], first['page'], first['pageSize']), (5, 1, 2))
        self.assertEqual(len(first['items']), 2)
        second = self._get(self.student, f'/api/projects/{pid}/tasks?pageSize=2&page=2').get_json()
        self.assertEqual(len(second['items']), 2)
        self.assertNotEqual([t['id'] for t in first['items']], [t['id'] for t in second['items']])
        empty = self._get(self.student, f'/api/projects/{pid}/tasks?pageSize=2&page=9').get_json()
        self.assertEqual((empty['items'], empty['total']), ([], 5))
        # 过滤同样在 SQL 层完成，total 是过滤后的数量
        doing = self._get(self.student, f'/api/projects/{pid}/tasks?status=doing&pageSize=10').get_json()
        self.assertEqual(doing['total'], 0)
        assigned = self._get(self.student, f'/api/projects/{pid}/tasks?assigneeId=u1&pageSize=10').get_json()
        self.assertEqual(assigned['total'], 2)
        # 非法 pageSize 回落到默认值而不是报错
        self.assertEqual(self._get(self.student, f'/api/projects/{pid}/tasks?pageSize=abc')
                         .get_json()['pageSize'], 100)
        self.assertEqual(self._get(self.student, f'/api/projects/{pid}/tasks?pageSize=99999')
                         .get_json()['pageSize'], 200)

    # ------------------------------------------------------------ 专注关联任务

    def test_focus_session_links_to_task(self):
        """番茄钟可关联任务：时长成为任务上的过程证据，且只能关联自己参与项目的任务。"""
        task = self.create_task(self.student, 'p1', title='可专注的任务')
        res = self._post(self.student, '/api/focus-sessions',
                         {'durationMin': 25, 'type': 'focus', 'taskId': task['id']})
        self.assertEqual(res.status_code, 201, res.get_json())
        self.assertEqual(res.get_json()['taskId'], task['id'])
        other = self.create_project(self.student2, name='别人的项目')
        foreign = self.create_task(self.student2, other['id'], title='别人的任务')
        self.assert_error(self._post(self.student, '/api/focus-sessions',
                                     {'durationMin': 25, 'taskId': foreign['id']}),
                          400, 'VALIDATION_ERROR')
        self.assert_error(self._post(self.student, '/api/focus-sessions',
                                     {'durationMin': 25, 'taskId': 123}), 400, 'VALIDATION_ERROR')
        listed = next(t for t in self._get(self.student, '/api/projects/p1/tasks').get_json()['items']
                      if t['id'] == task['id'])
        self.assertEqual(listed['focusMinutes'], 25)
        member = next(m for m in self._get(self.student, '/api/projects/p1/assessment')
                      .get_json()['members'] if m['user']['id'] == 'u1')
        self.assertEqual(member['focusMinutes'], 25)
        self._post(self.student, '/api/focus-sessions',
                   {'durationMin': 5, 'type': 'break', 'taskId': task['id']})
        listed = next(t for t in self._get(self.student, '/api/projects/p1/tasks').get_json()['items']
                      if t['id'] == task['id'])
        self.assertEqual(listed['focusMinutes'], 25)

    # ------------------------------------------------------------ 同伴互评

    def test_peer_review(self):
        """成员可对队友提交的成果给出认可或疑问；不能评自己，不能评未提交的任务。"""
        task = self.create_task(self.student, 'p1', title='互评对象')
        self.assert_error(self._post(self.student2, f"/api/tasks/{task['id']}/reviews",
                                     {'verdict': 'acknowledge'}), 409, 'TASK_NOT_SUBMITTED')
        for body in ({'assigneeId': 'u1'}, {'status': 'doing'}, {'status': 'review'}):
            self._patch(self.student, f"/api/tasks/{task['id']}", body)

        self.assert_error(self._post(self.student, f"/api/tasks/{task['id']}/reviews",
                                     {'verdict': 'acknowledge'}), 403, 'FORBIDDEN')
        self.assert_error(self._post(self.student2, f"/api/tasks/{task['id']}/reviews",
                                     {'verdict': 'question'}), 400, 'VALIDATION_ERROR')
        self.assert_error(self._post(self.student2, f"/api/tasks/{task['id']}/reviews",
                                     {'verdict': 'nope', 'comment': 'x'}), 400, 'VALIDATION_ERROR')

        first = self._post(self.student2, f"/api/tasks/{task['id']}/reviews",
                           {'verdict': 'question', 'comment': '数据来源没写'})
        self.assertEqual(first.status_code, 201, first.get_json())
        self.assertEqual(first.get_json()['reviewerName'], '李四')
        self._post(self.student2, f"/api/tasks/{task['id']}/reviews",
                   {'verdict': 'acknowledge', 'comment': '已确认'})
        items = self._get(self.student, f"/api/tasks/{task['id']}/reviews").get_json()['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['verdict'], 'acknowledge')

        listed = next(t for t in self._get(self.student, '/api/projects/p1/tasks').get_json()['items']
                      if t['id'] == task['id'])
        self.assertEqual(listed['peerReviewCount'], 1)
        peer = self._get(self.student, '/api/projects/p1/assessment').get_json()['peerReview']
        self.assertEqual(peer['total'], 1)
        self.assertEqual(peer['acknowledge'], 1)
        self.assert_error(self._get(self.student4, f"/api/tasks/{task['id']}/reviews"),
                          403, 'FORBIDDEN')

    # ------------------------------------------------------------ 教师量化评分

    def test_teacher_evaluation(self):
        """教师按固定维度给成员打分：维度必须齐全、分值有范围、学生只看自己那份。"""
        dims = {'problem': 4, 'solution': 5, 'collaboration': 3, 'presentation': 4}
        self.assert_error(self._put(self.student, '/api/projects/p1/evaluations/u1',
                                    {'dimensions': dims}), 403, 'FORBIDDEN')
        self.assert_error(self._put(self.teacher, '/api/projects/p1/evaluations/u1',
                                    {'dimensions': {'problem': 4}}), 400, 'VALIDATION_ERROR')
        self.assert_error(self._put(self.teacher, '/api/projects/p1/evaluations/u1',
                                    {'dimensions': {**dims, 'unknown': 3}}), 400, 'VALIDATION_ERROR')
        self.assert_error(self._put(self.teacher, '/api/projects/p1/evaluations/u1',
                                    {'dimensions': {**dims, 'problem': 9}}), 400, 'VALIDATION_ERROR')
        self.assert_error(self._put(self.teacher, '/api/projects/p1/evaluations/u1',
                                    {'dimensions': {**dims, 'problem': True}}), 400, 'VALIDATION_ERROR')
        self.assert_error(self._put(self.teacher, '/api/projects/p1/evaluations/nobody',
                                    {'dimensions': dims}), 404, 'NOT_FOUND')

        saved = self._put(self.teacher, '/api/projects/p1/evaluations/u1',
                          {'dimensions': dims, 'comment': '方案清晰'})
        self.assertEqual(saved.status_code, 200, saved.get_json())
        body = saved.get_json()
        self.assertEqual(body['total'], 16)
        self.assertEqual(body['maxTotal'], 20)
        self.assertEqual(body['dimensions']['solution'], 5)
        again = self._put(self.teacher, '/api/projects/p1/evaluations/u1',
                          {'dimensions': {**dims, 'problem': 5}})
        self.assertEqual(again.get_json()['total'], 17)
        self.assertEqual(len(self._get(self.teacher, '/api/projects/p1/evaluations').get_json()['items']), 1)

        self._put(self.teacher, '/api/projects/p1/evaluations/u2', {'dimensions': dims})
        self.assertEqual(len(self._get(self.teacher, '/api/projects/p1/evaluations').get_json()['items']), 2)
        own = self._get(self.student, '/api/projects/p1/evaluations').get_json()['items']
        self.assertEqual([e['userId'] for e in own], ['u1'])
        self.assertEqual(len(self._get(self.student2, '/api/projects/p1/evaluations').get_json()['items']), 1)

        evaluation = self._get(self.student, '/api/projects/p1/assessment').get_json()['evaluation']
        self.assertEqual(evaluation['count'], 2)
        self.assertEqual(evaluation['dimensions']['problem'], 4.5)
        # 归档需要项目已结题：用一个任务完成即可结题的新项目单独验证
        pid = self.create_project(self.student, name='评分归档')['id']
        self._put(self.teacher, f'/api/projects/{pid}/evaluations/u1', {'dimensions': dims})
        task = self.create_task(self.student, pid, title='归档任务')
        self.complete_task(task['id'], self.student)
        self.assertEqual(self._patch(self.student, f'/api/projects/{pid}', {'status': 'finished'}).status_code, 200)
        archive = self._get(self.student, f'/api/projects/{pid}/archive').get_json()
        self.assertEqual(len(archive['evaluations']), 1)
        self.assertEqual(archive['evaluations'][0]['evaluatorName'], '王老师')
        self.assertEqual(archive['assessment']['evaluation']['count'], 1)
        self.assertEqual(self._delete(self.teacher, '/api/projects/p1/evaluations/u1').status_code, 204)
        self.assertEqual(len(self._get(self.teacher, '/api/projects/p1/evaluations').get_json()['items']), 1)
        self.assert_error(self._delete(self.teacher, '/api/projects/p1/evaluations/u1'), 404, 'NOT_FOUND')

    # ------------------------------------------------------------ 通知中心与教学总览

    def test_notifications(self):
        """通知由既有数据派生：未读按游标计算，标记已读后清零。"""
        before = self._get(self.student, '/api/notifications').get_json()
        self.assertGreater(before['total'], 0)
        self.assertEqual(before['unread'], before['total'])

        self.assertEqual(self._post(self.student, '/api/notifications/seen', {}).status_code, 200)
        after = self._get(self.student, '/api/notifications').get_json()
        self.assertEqual(after['unread'], 0)
        self.assertEqual(after['total'], before['total'])
        self.assert_error(self._post(self.student, '/api/notifications/seen', {'seenAt': 123}),
                          400, 'VALIDATION_ERROR')

        self._post(self.teacher, '/api/projects/p1/annotations', {'content': '新批注'})
        fresh = self._get(self.student, '/api/notifications').get_json()
        self.assertEqual(fresh['unread'], 1)
        self.assertEqual([n['type'] for n in fresh['items'] if n['unread']], ['annotation'])

        task = self.create_task(self.student, 'p1', title='会被退回')
        for body in ({'assigneeId': 'u1'}, {'status': 'doing'}, {'status': 'review'}):
            self._patch(self.student, f"/api/tasks/{task['id']}", body)
        self._post(self.student, '/api/notifications/seen', {})
        self._patch(self.teacher, f"/api/tasks/{task['id']}", {'status': 'doing'})
        types = [n['type'] for n in self._get(self.student, '/api/notifications').get_json()['items']]
        self.assertIn('returned', types)

        verify = [n for n in self._get(self.teacher, '/api/notifications').get_json()['items']
                  if n['type'] == 'verify']
        self.assertTrue(verify)

    def test_teacher_analytics_scope(self):
        """教学总览聚合可见项目的过程评价；学生不可访问，校管理员限定本校。"""
        self.assert_error(self._get(self.student, '/api/teacher/analytics'), 403, 'FORBIDDEN')
        report = self._get(self.teacher, '/api/teacher/analytics').get_json()
        self.assertGreaterEqual(report['total'], 1)
        self.assertIn('withRisks', report)
        first = report['items'][0]
        for key in ('project', 'summary', 'warningCount', 'infoCount', 'topRisks'):
            self.assertIn(key, first)
        warnings = [row['warningCount'] for row in report['items']]
        self.assertEqual(warnings, sorted(warnings, reverse=True))
        # 新建项目出现在总览里，结题后消失（用新项目，避免受种子项目任务状态影响）
        pid = self.create_project(self.student, name='总览用项目')['id']
        listed = self._get(self.teacher, '/api/teacher/analytics').get_json()
        self.assertIn(pid, [row['project']['id'] for row in listed['items']])
        task = self.create_task(self.student, pid, title='结题用任务')
        self.complete_task(task['id'], self.student)
        self.assertEqual(self._patch(self.student, f'/api/projects/{pid}', {'status': 'finished'}).status_code, 200)
        after = self._get(self.teacher, '/api/teacher/analytics').get_json()
        self.assertNotIn(pid, [row['project']['id'] for row in after['items']])
        scoped = self._get(self.schooladmin, '/api/teacher/analytics').get_json()
        self.assertEqual(scoped['scopedToSchool'], 'sch1')

    # ------------------------------------------------------------ 通知的归属与时间戳

    def test_notifications_ignore_own_transitions(self):
        """只有"别人"把任务改回进行中才算被退回；自己认领/撤回不产生退回通知。"""
        task = self.create_task(self.student, 'p1', title='自己的任务')
        for body in ({'assigneeId': 'u1'}, {'status': 'doing'}):
            self._patch(self.student, f"/api/tasks/{task['id']}", body)
        self._post(self.student, '/api/notifications/seen', {})
        self._patch(self.student, f"/api/tasks/{task['id']}", {'status': 'review'})
        self._patch(self.student, f"/api/tasks/{task['id']}", {'status': 'doing'})  # 自己撤回
        types = [(n['type'], n['title']) for n in
                 self._get(self.student, '/api/notifications').get_json()['items']]
        self.assertEqual([t for t in types if t[0] == 'returned'], [], '自己的操作不应报成被退回')

        # 教师退回：出现且只有一条（重复退回不刷屏）
        self._post(self.student, '/api/notifications/seen', {})
        for _ in range(2):
            self._patch(self.student, f"/api/tasks/{task['id']}", {'status': 'review'})
            self._patch(self.teacher, f"/api/tasks/{task['id']}", {'status': 'doing'})
        returned = [n for n in self._get(self.student, '/api/notifications').get_json()['items']
                    if n['type'] == 'returned']
        self.assertEqual(len(returned), 1, f'同一任务只应有一条退回通知：{returned}')
        self.assertIn('王老师', returned[0]['detail'])
        self.assertTrue(returned[0]['unread'])

    def test_overdue_and_quiz_notification_timestamps(self):
        """逾期与错题提醒用"事件真正发生的时刻"，这样未读能清零、排序也不失真。"""
        # 未读的语义是"标记已读之后新发生的事"：因此用"刚刚到期"的任务验证未读，
        # 而已逾期很久的任务只应出现在列表里（用户早就被告知过）
        just_past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        task = self.create_task(self.student, 'p1', title='刚逾期任务')
        self._patch(self.student, f"/api/tasks/{task['id']}", {'assigneeId': 'u1'})
        old = self.create_task(self.student, 'p1', title='早就逾期')
        self._patch(self.student, f"/api/tasks/{old['id']}", {'assigneeId': 'u1'})
        self._patch(self.student, f"/api/tasks/{old['id']}", {'dueDate': '2020-01-01'})
        # 把游标设到过去，再把截止时间设在游标之后：等价于"标记已读之后才逾期"，
        # 不必真的等时间流逝
        cursor = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace('+00:00', 'Z')
        self.assertEqual(self._post(self.student, '/api/notifications/seen',
                                    {'seenAt': cursor}).status_code, 200)
        self._patch(self.student, f"/api/tasks/{task['id']}", {'dueDate': just_past})

        feed = self._get(self.student, '/api/notifications').get_json()
        by_title = {n['title']: n for n in feed['items'] if n['type'] == 'due'}
        self.assertIn('任务「刚逾期任务」已逾期', by_title)
        self.assertTrue(by_title['任务「刚逾期任务」已逾期']['unread'], '刚发生的事件应为未读')
        # 早就逾期的仍在列表里，但不再标记为未读
        self.assertIn('任务「早就逾期」已逾期', by_title)
        self.assertFalse(by_title['任务「早就逾期」已逾期']['unread'])

        # 到期未完成但未逾期的任务不发提醒（避免未来的时间戳永远清不掉未读）
        later = self.create_task(self.student, 'p1', title='还没到期')
        self._patch(self.student, f"/api/tasks/{later['id']}", {'assigneeId': 'u1'})
        self._patch(self.student, f"/api/tasks/{later['id']}", {'dueDate': '2099-01-01'})
        titles = [n['title'] for n in self._get(self.student, '/api/notifications').get_json()['items']]
        self.assertNotIn('还没到期', ' '.join(titles))

        # 错题提醒：时间戳取"最早一道题的到期时刻"，且该时刻在游标之后即为未读
        self.play_round(self.student, correct=0, count=3)
        due_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')
        with self.app.app_context():
            execute('UPDATE quiz_wrong_answers SET nextReviewAt = ? WHERE userId = ?', (due_at, 'u1'))
            commit()
        quiz = [n for n in self._get(self.student, '/api/notifications').get_json()['items']
                if n['type'] == 'quiz']
        self.assertEqual(len(quiz), 1, quiz)
        self.assertEqual(quiz[0]['createdAt'], due_at, '时间戳应是到期的时刻，而不是错题的创建时间')
        self.assertTrue(quiz[0]['unread'])

    def test_unread_matches_returned_items(self):
        """未读数与实际返回的条目一致：徽标上的数字要能在列表里对上。"""
        feed = self._get(self.student, '/api/notifications').get_json()
        self.assertEqual(feed['unread'], sum(1 for n in feed['items'] if n['unread']))
        self.assertEqual(feed['total'], len(feed['items']))
        # 标记已读后归零
        self._post(self.student, '/api/notifications/seen', {})
        after = self._get(self.student, '/api/notifications').get_json()
        self.assertEqual(after['unread'], 0)

    def test_evaluation_summary_weights_members_once(self):
        """评分汇总按成员汇总后再平均：被两位教师评过的人不应被算两遍。"""
        dims_high = {'problem': 5, 'solution': 5, 'collaboration': 5, 'presentation': 5}
        dims_low = {'problem': 1, 'solution': 1, 'collaboration': 1, 'presentation': 1}
        self._put(self.teacher, '/api/projects/p1/evaluations/u1', {'dimensions': dims_high})
        self._put(self.schooladmin, '/api/projects/p1/evaluations/u1', {'dimensions': dims_low})
        self._put(self.teacher, '/api/projects/p1/evaluations/u2', {'dimensions': dims_high})

        summary = self._get(self.teacher, '/api/projects/p1/assessment').get_json()['evaluation']
        self.assertEqual(summary['count'], 3, '三条评分记录')
        self.assertEqual(summary['members'], 2, '涉及两名成员')
        # 张三：(20+4)/2 = 12，李四：20 → 均分 16；若按条平均会得到 14.7
        self.assertEqual(summary['avgTotal'], 16.0)
        self.assertEqual(summary['dimensions']['problem'], 4.0)

    def test_focus_link_survives_and_criteria_edit_updates_log(self):
        """验收标准更新后，同一次请求里的状态流转日志要读到新清单。"""
        task = self.create_task(self.student, 'p1', title='标准与日志')
        self._patch(self.student, f"/api/tasks/{task['id']}", {'assigneeId': 'u1'})
        # 状态必须逐级推进：跳过"进行中"直接提交验收会被拒
        self._patch(self.student, f"/api/tasks/{task['id']}", {'status': 'doing'})
        self._patch(self.student, f"/api/tasks/{task['id']}", {'status': 'review'})
        self._patch(self.teacher, f"/api/tasks/{task['id']}", {
            'status': 'done',
            'criteria': [{'text': 'A', 'done': True}, {'text': 'B', 'done': True}],
        })
        logs = [row['detail'] for row in
                self._get(self.student, '/api/projects/p1/task-logs').get_json()['items']]
        self.assertIn('状态更新为 已完成（验收标准 2/2 项达标）', logs)

    # ------------------------------------------------------------ 字段边界

    def test_board_and_checkin_field_limits(self):
        note = self._post(self.student, '/api/projects/p1/notes', {'content': '灵感'}).get_json()
        self.assertEqual(note['x'], 20)  # 缺省落点
        for body in ({'content': 'x' * 2001}, {'color': 'y' * 21}, {'x': 999999}):
            self.assert_error(self._patch(self.student, f"/api/notes/{note['id']}", body), 400, 'VALIDATION_ERROR')
        self.assertEqual(
            self._patch(self.student, f"/api/notes/{note['id']}", {'content': 'x' * 2000, 'x': 20000}).status_code, 200)

        self.assert_error(self._post(self.student, '/api/projects/p1/notes', {'content': 'x' * 2001}),
                          400, 'VALIDATION_ERROR')
        self.assert_error(self._post(self.student, '/api/projects/p1/mind-nodes', {'label': 'x' * 201}),
                          400, 'VALIDATION_ERROR')
        self.assert_error(self._post(self.student, '/api/projects/p1/checkins', {'content': 'x' * 2001}),
                          400, 'VALIDATION_ERROR')
        self.assert_error(self._post(self.teacher, '/api/projects/p1/annotations', {'content': 'x' * 2001}),
                          400, 'VALIDATION_ERROR')

        for bad in (241, 'x', float('inf')):
            self.assert_error(self._post(self.student, '/api/focus-sessions', {'durationMin': bad}),
                              400, 'VALIDATION_ERROR')
        self.assertEqual(self._post(self.student, '/api/focus-sessions', {'durationMin': 240}).status_code, 201)


if __name__ == '__main__':
    unittest.main(verbosity=2)
