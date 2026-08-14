"""InnoArk 后端端到端测试（Flask test client + 临时 SQLite）。

运行：python -m unittest discover -s tests -v
每个用例使用独立临时数据库（种子数据保持一致），互不影响。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.services import FEEDBACK_POOL  # noqa: E402


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.app = create_app({'DATABASE': self.db_path})
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
        res = self.client.post('/api/users', json={
            'username': username, 'password': '123456', 'name': '李老师', 'role': 'teacher',
        })
        self.assertEqual(res.status_code, 201, res.get_json())
        return self._login(username, '123456')

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
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()['user']['role'], 'teacher')

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
        res = self._patch(self.student, '/api/projects/p1', {'status': 'finished'})
        body = res.get_json()
        self.assertEqual(body['status'], 'finished')
        self.assertIsNotNone(body['finishedAt'])
        # 结题生成里程碑反馈
        res = self._get(self.student, '/api/projects/p1/feedbacks')
        feedbacks = res.get_json()['items']
        self.assertEqual(feedbacks[0]['type'], 'milestone')
        self.assertIn('结题', feedbacks[0]['content'])
        # 结题后档案可访问
        res = self._get(self.student, '/api/projects/p1/archive')
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
        for status in ('doing', 'review', 'done'):
            res = self._patch(self.student, f"/api/tasks/{task['id']}", {'status': status})
            self.assertEqual(res.status_code, 200, res.get_json())
        # 动态（按时间倒序）：3 次 status + create
        logs = self._get(self.student, '/api/projects/p1/task-logs').get_json()['items']
        task_logs = [l for l in logs if l['taskId'] == task['id']]
        self.assertEqual([l['action'] for l in task_logs], ['status', 'status', 'status', 'create'])
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
        # 教师对协作内容只读：写操作全部 403
        res = self._post(self.teacher, '/api/projects/p1/tasks', {'title': 'x'})
        self.assert_error(res, 403, 'FORBIDDEN')
        res = self._post(self.teacher, '/api/projects/p1/checkins', {'content': 'x'})
        self.assert_error(res, 403, 'FORBIDDEN')
        res = self._patch(self.teacher, '/api/tasks/t1', {'status': 'done'})
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
             'question', 'options', 'answer', 'explanation'})
        self.assertIsNone(q['groupId'])
        self.assertEqual(len(q['options']), 4)
        self.assertIsInstance(q['answer'], int)
        # count 上限 20，非法值回落默认 10
        res = self._get(self.student, '/api/quiz/questions?count=999')
        self.assertEqual(len(res.get_json()['items']), 20)
        res = self._get(self.student, '/api/quiz/questions?count=abc')
        self.assertEqual(len(res.get_json()['items']), 10)

    def test_quiz_attempts_and_stats(self):
        res = self._post(self.student, '/api/quiz/attempts', {'score': 8, 'total': 10})
        self.assertEqual(res.status_code, 201)
        body = res.get_json()
        self.assertEqual(body['attempt']['score'], 8)
        self.assertEqual(body['best']['score'], 8)
        res = self._post(self.student, '/api/quiz/attempts', {'score': 10, 'total': 10})
        self.assertEqual(res.get_json()['best']['score'], 10)
        stats = self._get(self.student, '/api/quiz/stats').get_json()
        self.assertEqual(stats['attempts'], 2)
        self.assertEqual(stats['best']['score'], 10)
        self.assertEqual(stats['last']['score'], 10)
        # 不同用户成绩互不干扰
        self._post(self.student2, '/api/quiz/attempts', {'score': 2, 'total': 10})
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
        gid = self._post(self.teacher, '/api/groups', {'name': '新组'}).get_json()['id']
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
