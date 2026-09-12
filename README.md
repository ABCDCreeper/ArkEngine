# ArkEngine — 智创方舟 (InnoArk)

InnoArk 的 Flask 后端，比赛版本实现服务端计分、任务验收、结题只读快照与可解释过程评价。

## 核心功能

- **用户系统**: 注册/登录（token 鉴权），学生/教师/管理员多角色，公开注册仅限学生
- **课题与项目**: 课题库浏览、发起项目、邀请码组队（≤ 4 人）、跨组隔离
- **星云创意看板**: 多人协同思维导图 + 灵感便签
- **PBL 任务看板**: 认领 / 状态流转 / 动态记录，任务完成自动打卡 + 里程碑反馈
- **教师量化评分**: 四维度（问题理解、方案与创新、协作与过程、表达与呈现）1-5 分评分
- **同伴互评**: 学生间认可 / 疑问的互评机制
- **过程评价引擎**: 可解释的过程指标、趋势对比、风险预警（6 种预警类型）、教学总览
- **每日打卡**与系统动态反馈（思路引导）
- **跨学科资源导航**（分类 + 关键词搜索）
- **沉浸式专注模式**: 番茄钟上报 + 近 N 天专注统计
- **教师端**: 全部团队总览、在线批注、分组管理
- **知识闯关**: 公共题库 + 组内题库，支持 fallback / mixed 模式
- **管理后台**: 分层用户管理（校管理员/管理员/超级管理员）+ 审计日志
- **成果归档**: 结题后自动生成科创档案
- **通知中心**: 批注、被退回任务、到期任务、互评、邀请、错题复习等通知
- **错题本**: 服务端记录错题，支持复习

## 技术栈

- Python 3.10+ / Flask 3
- SQLite（标准库 `sqlite3`，无 ORM）
- 鉴权：服务端 session token（过期机制，登出即失效）
- 过程评价：可配置的指标权重与风险规则
- E2E 测试：Playwright

## 部署指令

```bash
pip install -r requirements.txt   # 安装依赖
python run.py                     # 启动 http://127.0.0.1:5000
python -m unittest discover -s tests -v   # 运行后端测试
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ARK_DATABASE` | `instance/innoark.db` | 数据库路径 |
| `ARK_FRONTEND_DIST` | - | 前端构建产物路径（可选） |
| `ARK_PASSWORD_HASH` | - | 测试用哈希算法（如 `plaintext`） |
| `FLASK_DEBUG` | `0` | 调试模式 |
| `HOST` / `PORT` | `127.0.0.1:5000` | 监听地址 |

- 首次启动自动建表并写入演示数据（`instance/innoark.db`，删除文件可重置）
- 演示账号（密码统一 `123456`）：`student` / `student2` / `student3` / `student4`（学生）、`teacher`（教师）

### E2E 测试

```bash
cd e2e
npm install
npx playwright test
```

## 项目结构

```
app/                    # Flask 后端应用
├── routes/             #   路由模块（13 个）
│   ├── auth.py         #     认证
│   ├── projects.py     #     课题与项目
│   ├── tasks.py        #     PBL 任务
│   ├── kanban.py       #     星云看板
│   ├── quiz.py         #     知识闯关
│   ├── groups.py       #     用户分组
│   ├── teacher.py      #     教师端
│   ├── admin.py        #     管理员
│   ├── insights.py     #     通知中心 + 教学总览
│   ├── review.py       #     同伴互评 + 教师评分
│   └── ...             #     其他
├── assessment.py       # 过程评价引擎
├── security.py         # 口令哈希（可配置）
├── validation.py       # 参数校验工具
├── frontend.py         # 前端静态文件分发
├── services.py         # 共享业务逻辑
└── db.py               # SQLite 数据层
e2e/                    # Playwright 端到端测试
tests/                  # 后端单元测试
```

## 前端对接

前端 `vite.config.ts` 移除 `mockPlugin()` 并配置代理：

```ts
server: {
  proxy: { '/api': { target: 'http://localhost:5000', changeOrigin: true } }
}
```

或将前端构建产物路径设为 `ARK_FRONTEND_DIST`，由后端直接 serve。