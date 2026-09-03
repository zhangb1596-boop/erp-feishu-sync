"""
========================================
ERP → 飞书多维表 数据同步平台 · 管理端
========================================
端口：5001
功能：任务看板 API、仪表盘、执行历史、配置管理、一键同步

说明：本文件是从内部「金蝶苍穹 → 飞书」数据中台提炼的 Demo 骨架。
      所有与真实 ERP / 飞书相关的业务模块 import 均已移除，
      业务线通过 sync_handlers.register_handler 注册、可插拔。
"""
import sys
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream and not _stream.closed:
            _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import os
import uuid
import re
import datetime
import logging
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import task_store as db
import scheduler_service as scheduler
import sync_handlers as handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ==================== 启动 / 关闭 ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 50)
    logger.info("管理端启动，加载任务到调度器...")
    scheduler.load_all_tasks()
    logger.info("=" * 50)
    yield
    logger.info("管理端关闭")


app = FastAPI(
    title="ERP → 飞书多维表 同步管理端",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== 请求 / 响应模型 ====================

class TaskCreateBody(BaseModel):
    name: str
    biz_type: str = "采购订单"
    mode: str                     # interval | daily | multi_daily
    interval_seconds: Optional[int] = 3600
    daily_times: Optional[list] = []
    daily_time: Optional[str] = "09:00"
    start_date: str
    end_date: str
    use_today_as_end_date: bool = False
    verify: bool = True
    retry_times: int = 3
    retry_interval: int = 60
    sync_target_id: Optional[str] = None


class SyncTargetCreateBody(BaseModel):
    target_id: Optional[str] = None
    name: str
    biz_type: str
    app_token: str
    table_id: str
    auto_create_fields: bool = True


# ==================== 通用工具 ====================

def ok(data=None, message="success"):
    return {"code": 0, "message": message, "data": data}


def fail(code=400, message="error", data=None):
    return {"code": code, "message": message, "data": data}


def validate_date(s: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", s))


# ==================== 静态页面 ====================

HTML_PATH = os.path.join(os.path.dirname(__file__), "admin.html")


@app.get("/", response_class=FileResponse)
async def root():
    return HTML_PATH


@app.get("/admin.html", response_class=FileResponse)
async def admin_frontend():
    return HTML_PATH


# ==================== 健康检查 ====================

@app.get("/health")
async def health():
    return ok({
        "service": "ERP → 飞书 同步管理端",
        "version": "1.0.0",
        "scheduler": scheduler.get_scheduler_status(),
        "db_tasks_count": len(db.get_all_tasks()),
        "registered_biz_types": handlers.registered_biz_types(),
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.get("/api/health/business")
async def business_health():
    """探测 ERP 与飞书连接状态（Demo 返回模拟结果；真实环境替换为实际连通性探测）"""
    result = {
        "erp": handlers.probe_erp_connection(),
        "feishu": handlers.probe_feishu_connection(),
    }
    return ok(result)


# ==================== 任务 CRUD ====================

@app.get("/api/tasks")
async def list_tasks():
    tasks = db.get_all_tasks()
    for t in tasks:
        t["next_run"] = None
        t["is_running"] = False
        t["status"] = "stopped" if not t["enabled"] else "pending"
    return ok(tasks)


@app.post("/api/tasks")
async def create_task(body: TaskCreateBody):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="任务名称不能为空")
    if not validate_date(body.start_date) or not validate_date(body.end_date):
        raise HTTPException(status_code=400, detail="日期格式错误，请使用 YYYY-MM-DD")
    if body.start_date > body.end_date:
        raise HTTPException(status_code=400, detail="开始日期不能大于结束日期")
    if body.biz_type not in handlers.registered_biz_types():
        raise HTTPException(
            status_code=400,
            detail=f"未知业务线: {body.biz_type}，可选: {handlers.registered_biz_types()}",
        )

    # mode 校验
    if body.mode == "interval":
        if not body.interval_seconds or body.interval_seconds < 60:
            raise HTTPException(status_code=400, detail="间隔不能少于 60 秒")
    elif body.mode == "daily":
        if not re.match(r"^\d{2}:\d{2}$", body.daily_time or ""):
            raise HTTPException(status_code=400, detail="daily_time 格式错误，请使用 HH:MM")
    elif body.mode == "multi_daily":
        if not body.daily_times:
            raise HTTPException(status_code=400, detail="multi_daily 模式需要提供 daily_times")
        for t in body.daily_times:
            if not re.match(r"^\d{2}:\d{2}$", str(t)):
                raise HTTPException(status_code=400, detail=f"时间格式错误: {t}，请使用 HH:MM")
    else:
        raise HTTPException(status_code=400, detail="mode 必须是 interval / daily / multi_daily")

    task_id = str(uuid.uuid4())[:8]

    db.save_task(
        task_id=task_id,
        name=body.name,
        task_type="feishu_sync",
        biz_type=body.biz_type,
        mode=body.mode,
        interval_seconds=body.interval_seconds or 3600,
        daily_times=body.daily_times or [],
        daily_time=body.daily_time or "09:00",
        start_date=body.start_date,
        end_date=body.end_date,
        use_today_as_end_date=body.use_today_as_end_date,
        verify=body.verify,
        retry_times=body.retry_times,
        retry_interval=body.retry_interval,
        sync_target_id=body.sync_target_id,
        enabled=False,
    )

    db.add_operation_log("创建任务", task_id, f"创建任务: {body.name} ({body.mode})")
    task = db.get_task(task_id)
    return ok(task, "任务已创建")


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return ok(task)


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    scheduler.stop_task(task_id)
    if not db.delete_task(task_id):
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    db.add_operation_log("删除任务", task_id, f"删除任务: {task_id}")
    return ok(message="任务已删除")


# ==================== 任务控制 ====================

@app.post("/api/tasks/{task_id}/start")
async def start_task(task_id: str):
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if scheduler.start_task(task_id):
        return ok(message="任务已启动")
    raise HTTPException(status_code=500, detail="启动失败")


@app.post("/api/tasks/{task_id}/stop")
async def api_stop_task(task_id: str):
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if scheduler.stop_task(task_id):
        return ok(message="任务已停止")
    raise HTTPException(status_code=500, detail="停止失败")


@app.post("/api/tasks/{task_id}/trigger")
async def trigger_sync(task_id: str):
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if scheduler.trigger_task(task_id):
        return ok(message="已触发一次执行")
    raise HTTPException(status_code=500, detail="触发失败")


# ==================== 执行历史 ====================

@app.get("/api/history")
async def list_history(
    task_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    if task_id:
        records = db.get_history(task_id, limit)
    else:
        records = db.get_all_history(limit)
    return ok(records)


@app.get("/api/history/stats")
async def history_stats(days: int = Query(7, ge=1, le=90)):
    return ok(db.get_history_stats(days))


@app.delete("/api/history")
async def clear_history(task_id: Optional[str] = Query(None)):
    count = db.clear_history(task_id)
    db.add_operation_log("清空历史", task_id or "all", f"清空历史记录 {count} 条")
    return ok({"count": count}, "已清空历史记录")


# ==================== 操作日志 ====================

@app.get("/api/operation_log")
async def list_operation_log(limit: int = Query(100, ge=1, le=500)):
    return ok(db.get_operation_log(limit))


# ==================== 调度器状态 ====================

@app.get("/api/scheduler/status")
async def scheduler_status():
    return ok(scheduler.get_scheduler_status())


# ==================== 同步目标配置 ====================

@app.get("/api/sync-targets")
async def list_sync_targets(biz_type: Optional[str] = Query(None)):
    return ok(db.get_all_sync_targets(biz_type))


@app.post("/api/sync-targets")
async def create_sync_target(body: SyncTargetCreateBody):
    target_id = body.target_id or str(uuid.uuid4())[:8]
    record = db.save_sync_target(
        target_id=target_id,
        name=body.name,
        biz_type=body.biz_type,
        app_token=body.app_token,
        table_id=body.table_id,
        auto_create_fields=body.auto_create_fields,
    )
    db.add_operation_log("保存同步目标", target_id, f"保存同步目标: {body.name}")
    return ok(record, "目标已保存")


@app.delete("/api/sync-targets/{target_id}")
async def delete_sync_target(target_id: str):
    if db.delete_sync_target(target_id):
        db.add_operation_log("删除同步目标", target_id, f"删除同步目标: {target_id}")
        return ok(message="目标已删除")
    raise HTTPException(status_code=404, detail="目标不存在")


@app.post("/api/sync-targets/test-connection")
async def test_target_connection(
    app_token: str = Query(...),
    table_id: str = Query(...),
):
    """测试目标表连通性（Demo 返回模拟结果）"""
    return ok(handlers.probe_feishu_connection(app_token, table_id))


# ==================== 一键同步（按业务线分发） ====================

@app.post("/api/sync/once")
async def sync_once(
    start_date: str = Query(...),
    end_date: str = Query(...),
    verify: bool = Query(True),
    biz_type: str = Query("采购订单"),
):
    if not validate_date(start_date) or not validate_date(end_date):
        raise HTTPException(status_code=400, detail="日期格式错误")
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="开始日期不能大于结束日期")
    if biz_type not in handlers.registered_biz_types():
        raise HTTPException(status_code=400, detail=f"未知业务线: {biz_type}")

    handler = handlers.get_handler(biz_type)
    result_container = {}
    error_container = [None]

    def _do():
        try:
            start_ts = datetime.datetime.now()
            # 1) 从数据源拉取（真实环境为 ERP Open API）
            raw = handler["fetcher"](start_date=start_date, end_date=end_date, verify=verify)
            if raw is None:
                error_container[0] = f"从 ERP 获取 {biz_type} 数据失败"
                return
            # 2) 字段口径映射（扁平化）
            rows = handler["mapper"](raw)
            # 3) 写入目标表（真实环境为飞书多维表 API）
            written, skipped = handler["writer"](rows, handler["row_key"])
            duration = round((datetime.datetime.now() - start_ts).total_seconds(), 1)
            result_container.update({
                "biz_type": biz_type,
                "fetched": len(raw),
                "written": written,
                "skipped": skipped,
                "duration": duration,
                "message": f"模拟完成，拉取 {len(raw)} 条、写入 {written} 条",
            })
        except Exception as e:
            error_container[0] = str(e)

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=300)

    if error_container[0]:
        raise HTTPException(status_code=500, detail=error_container[0])
    return ok(result_container, result_container.get("message", "同步完成"))


# ==================== 独立运行入口 ====================

if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 50)
    print("ERP × 飞书 - 同步管理端（Demo）")
    print("=" * 50)
    print("前端页面: http://localhost:5001/")
    print("API 文档: http://localhost:5001/docs")
    print("=" * 50 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=5001, log_level="info")
