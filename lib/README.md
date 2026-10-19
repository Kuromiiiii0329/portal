# lib/ — 捆绑的依赖副本

本目录是 `daily_stock_analysis` 主项目中 `src/` 与 `data_provider/` 的快照副本，目的是让 portal 脱离主项目后仍能独立运行。

| 路径 | 来源 | 说明 |
|------|------|------|
| `src/` | `daily_stock_analysis/src/` | 核心编排 / 配置 / 通知 / 报告 / schemas 等模块 |
| `data_provider/` | `daily_stock_analysis/data_provider/` | 多数据源适配与 fallback |
| `LICENSE` | `daily_stock_analysis/LICENSE` | 上游 MIT 许可证原文（版权归 ZhuLinsen） |

## 来源版本

- 源仓库：<https://github.com/Kuromiiiii0329/daily_stock_analysis>
- 源提交：`158aad9dda0eeebb35251dd094704696914ac096`
- 复制方式：整目录复制，排除 `__pycache__/` 与 `*.pyc`

## 运行时如何生效

`server.py` 最先导入 `srv._config`，该模块在顶层把本目录插入 `sys.path` 首位：

1. `portal/lib/` 优先 —— `from data_provider import ...`、`from src...` 解析到这里；
2. 其次是主项目根目录 —— 兼容仍在 `daily_stock_analysis/portal/` 内直接运行的场景。

## 维护约定

- 这是**快照副本**，不是 submodule。上游变更需要手工重新同步，并更新上面的「源提交」。
- 不要在本目录里直接改业务逻辑；改动应回到主项目，再重新复制过来。
