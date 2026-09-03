# ERP → 飞书多维表 数据同步平台 · 管理端（Demo）

> 从公司内部「金蝶苍穹 ERP → 飞书多维表」数据中台项目中提炼的**可运行骨架**。
> 剥离了真实 ERP/飞书凭据与内部业务模块，保留完整工程架构，用**内置模拟数据源**即可一键跑通。

## 这个 Demo 展示什么

| 能力 | 说明 |
| --- | --- |
| 任务调度 | 支持 `interval / daily / multi_daily` 三种调度模式 + 失败自动重试（APScheduler） |
| 业务线可插拔 | 新增一条业务线 = 注册一个「拉取 + 映射 + 写入」handler，调度与管理端**零改动** |
| 换目标不改代码 | 每条同步任务可绑定不同飞书表（app_token + table_id），配置化 |
| 同步策略 | 全量覆盖 / 明细行展开 / 行级幂等（用唯一键去重） |
| 可观测 | 执行历史、7 日成功率统计、操作日志、健康检查、业务连接状态 |
| 前端 | 管理端自带极简 HTML 看板（单文件、无框架） |

## 目录结构

```
erp-feishu-sync/
├── app.py                  # FastAPI 入口：路由 / 校验 / 静态页 / /api/sync/once 一键同步
├── task_store.py           # 数据层：JSON 文件持久化（任务 / 历史 / 日志 / 同步目标）
├── scheduler_service.py    # APScheduler 封装：start/stop/trigger、三种模式注册
├── sync_handlers.py        # ★ 业务线注册表 + 内置 2 条示例业务线（模拟数据源）
├── admin.html              # 管理端单页看板（任务列表 / 启停 / 历史 / 目标表配置）
├── requirements.txt
└── README.md
```

## 快速开始

```bash
pip install -r requirements.txt
python app.py
# 管理端页面  http://127.0.0.1:5001/
# Swagger 文档 http://127.0.0.1:5001/docs
```

浏览器打开后可以看到：

1. **业务连接检查** → `GET /api/health/business`（真实环境探测 ERP / 飞书连通性，Demo 返回模拟状态）
2. **创建任务** → `POST /api/tasks`（如 `demo`：采购订单 · daily · 09:00 · 近 7 天）
3. **一键同步** → `GET /api/sync/once?start_date=...&end_date=...`（立即跑一次，看到"模拟拉取 → 映射 → 写入"全过程）
4. **调度启停** → `POST /api/tasks/{id}/start`，在"看板 / 执行历史"里观察运行结果

## 如何接入你自己的 ERP / 飞书（替换模拟源）

只改 `sync_handlers.py` 一个文件即可：

```python
register_handler(
    biz_type="采购订单",
    fetcher=your_real_fetch,        # 换成真实 Open API 拉取函数
    mapper=lambda rows: rows,       # 字段映射 / 口径统一
    writer=your_feishu_writer,      # 换成真实飞书多维表写入函数
    row_key="bill_no",              # 幂等去重用的唯一键
)
```

> ⚠️ 接入真实环境时：把 `fetcher / writer` 换成调真实接口的实现，并在 `config` 里填
> ERP / 飞书的 app 凭据。**切勿把真实凭据提交到公开仓库。**

## 与内部原版的差异（脱敏说明）

- 移除金蝶苍穹 Token 管理、应付单/海运单/采购订单等内部业务获取与同步模块的 import
- 移除真实 base_url、app_token、tenant 等配置
- 内置 2 条**示例业务线**（采购订单 / 财务应付单）用 mock 数据演示同一套工程链路
- 保留：FastAPI 服务框架、任务 CRUD/启停/触发、执行历史与统计、同步目标配置、
  一键同步、调度器三种模式、前端看板 —— 架构与生产版一致
