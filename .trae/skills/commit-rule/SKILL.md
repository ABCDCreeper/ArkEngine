---
name: "commit-rule"
description: "ArkEngine 项目的 Git commit 规范。使用 Conventional Commits 格式，所有提交信息使用英文小写。当用户询问 commit 规范或需要编写 commit 信息时调用。"
---

# ArkEngine Commit Rule

## 格式

```
<type>: <小写英文描述>
```

不允许句尾句号。描述简洁不超过 80 字符。每条 commit 只做一个独立功能。

## type 类型

| type | 用途 | 示例 |
|------|------|------|
| `feat` | 新功能 | `feat: add user registration endpoint per updated API doc` |
| `fix` | 缺陷修复 | `fix: backfill admin demo accounts for existing databases` |
| `ci` | CI/CD 变更 | `ci: add syntax-check GitHub Action workflow` |
| `docs` | 文档 | `docs: rename project title to ArkEngine in README` |
| `style` | 代码风格 | `style: fix ruff lint errors (shadowed g import, unused variable)` |
| `tests` | 测试 | `tests: add backend unit tests with temporary SQLite database per test case` |

## 要点

- 全部英文小写
- 末尾不加句号
- 主体描述使用祈使句（如 "add"、"fix"、"implement"）
- 新功能的 commit 一条只做一个功能，不混合无关变更
- 如有需要，用 `()` 补充范围信息，如 `(shadowed g import)`