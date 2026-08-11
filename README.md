# InnoArk「虫洞·星桥」后端

智能跨学科项目式学习协同平台（InnoArk）的 Flask 后端，按前端 API 契约 [docs/api.md](../InnoArk/docs/api.md) 实现。

## 技术栈

- Python 3.10+ / Flask 3
- SQLite（标准库 `sqlite3`，无 ORM，仅依赖 flask）
- 鉴权：服务端 session token（登录签发、登出删除，满足契约「登出使 token 失效」）

## 快速开始

```bash
pip install -r requirements.txt
python run.py          # http://127.0.0.1:5000
```

首次启动自动建表并写入演示数据（`instance/innoark.db`，可删除后重置）。

演示账号（密码统一 `123456`）：`student` / `student2` / `student3` / `student4`（学生）、`teacher`（教师）。

## 运行测试

```bash
python -m unittest discover -s tests -v
```

31 个端到端用例，覆盖全部接口与权限模型（每个用例使用独立临时数据库）。

## 对接前端

前端（`InnoArk` 仓库）当前由 `mock/`（Vite 中间件）提供模拟数据。接入真实后端：

1. 修改 `InnoArk/vite.config.ts`：移除 `mockPlugin()`，配置代理：

```ts
server: {
  proxy: { '/api': { target: 'http://localhost:5000', changeOrigin: true } }
}
```

2. 前端代码无需任何改动（`src/api/request.ts` 中 `BASE_URL = '/api'`）。

## 项目结构

```
app/
  __init__.py      # create_app 工厂（JSON 中文原样输出）
  config.py        # 配置（数据库路径、队伍人数上限）
  db.py            # SQLite 连接、建表、种子数据（镜像 mock/db.ts）
  errors.py        # ApiError + 统一错误响应（契约 1.4）
  auth.py          # session token 签发/校验、require_auth 装饰器
  services.py      # 权限校验、资源视图、任务状态联动、分页
  routes/          # 蓝图：auth / projects / kanban / tasks / activity / resources / focus / teacher
tests/test_api.py  # 端到端测试
run.py             # 开发入口（HOST/PORT/FLASK_DEBUG 环境变量可覆盖）
```

## 实现要点

- 表结构对应契约 2.x 数据模型，列名与 JSON 字段一致（camelCase）。
- 权限模型（契约 1.6）：学生须为项目成员方可读写协作内容；教师只读协作内容、可写批注、可访问 `/api/teacher/*`。
- 任务状态变更的「动态 + 打卡 + 里程碑反馈」三连写在同一个 SQLite 事务中提交（契约 3.4）。
- 列表接口统一 `{ items, total, page, pageSize }`，默认 `pageSize=100`，支持 `?page=` / `?pageSize=`（契约 1.5）。
- 时间统一 ISO 8601（UTC，微秒精度），保证倒序排序稳定。
