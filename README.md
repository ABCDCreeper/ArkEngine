# InnoArk「虫洞·星桥」后端

智能跨学科项目式学习协同平台（InnoArk）的 Flask 后端，按前端 API 契约 [docs/api.md](../InnoArk/docs/api.md) 实现。

## 核心功能

- 用户注册 / 登录（token 鉴权），学生 / 教师双角色
- 课题库浏览、发起项目、邀请码组队（≤ 4 人）
- 星云创意看板：多人思维导图 + 灵感便签
- PBL 任务看板：认领 / 状态流转 / 动态记录，任务完成自动打卡 + 里程碑反馈
- 每日打卡与系统动态反馈（思路引导）
- 跨学科资源导航（分类 + 关键词搜索）
- 沉浸式专注模式：番茄钟上报 + 近 N 天专注统计
- 教师端：全部团队总览、在线批注
- 成果归档：结题后自动生成科创档案

## 技术栈

- Python 3.10+ / Flask 3
- SQLite（标准库 `sqlite3`，无 ORM，仅依赖 flask）
- 鉴权：服务端 session token（登出即失效）

## 部署指令

```bash
pip install -r requirements.txt   # 安装依赖
python run.py                     # 启动 http://127.0.0.1:5000
python -m unittest discover -s tests -v   # 运行测试
```

- 首次启动自动建表并写入演示数据（`instance/innoark.db`，删除文件可重置）
- 演示账号（密码统一 `123456`）：`student` / `student2` / `student3` / `student4`（学生）、`teacher`（教师）

### 对接前端

InnoArk 前端 `vite.config.ts` 移除 `mockPlugin()` 并配置代理：

```ts
server: {
  proxy: { '/api': { target: 'http://localhost:5000', changeOrigin: true } }
}
```
