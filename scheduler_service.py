"""
调度器服务：APScheduler 封装
支持三种调度模式：
  - interval   ：按固定间隔（最短 60 秒）
  - daily      ：每日一个固定时间点
  - multi_daily：每日多个时间点
附带：失败自动重试、启动时按数据库任务恢复、运行记录写回执行历史。
"""
import datetime
import logging
import threading
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

import task_store as db
import sync_handlers as handlers

logger = logging.getLogger(__name__)

_scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
_job_map = {}   # task_id -> job_id
_lock = threading.Lock()


def _run_task(task_id: str) -> None:
    """任务执行体：拉取 -> 映射 -> 写入，支持重试，记录历史"""
    task = db.get_task(task_id)
    if not task:
        logger.warning("任务不存在: %s", task_id)
        return

    biz_type = task.get("biz_type", "采购订单")
    handler = handlers.get_handler(biz_type)
    if not handler:
        logger.warning("业务线未注册: %s", biz_type)
        return

    retry_times = int(task.get("retry_times", 3))
    retry_interval = int(task.get("retry_interval", 60))
    verify = bool(task.get("verify", True))

    last_error = None
    for attempt in range(retry_times + 1):
        try:
            start_ts = datetime.datetime.now()
            raw = handler["fetcher"](
                start_date=task.get("start_date"),
                end_date=task.get("end_date"),
                verify=verify,
            )
            if raw is None:
                raise RuntimeError(f"从 ERP 获取 {biz_type} 数据失败")
            rows = handler["mapper"](raw)
            written, skipped = handler["writer"](rows, handler["row_key"])
            duration = round((datetime.datetime.now() - start_ts).total_seconds(), 1)

            db.add_history(task_id, task.get("name", task_id), biz_type, {
                "status": "success",
                "fetched": len(raw),
                "written": written,
                "skipped": skipped,
                "duration": duration,
                "message": f"成功：拉取 {len(raw)} 条 / 写入 {written} 条 / 跳过 {skipped} 条",
            })
            logger.info("[%s] %s 同步成功: %s", task_id, biz_type,
                        f"拉取{len(raw)} 写入{written} 耗时{duration}s")
            return
        except Exception as e:
            last_error = str(e)
            logger.warning("[%s] 第 %d 次尝试失败: %s", task_id, attempt + 1, e)
            if attempt < retry_times:
                import time as _time
                _time.sleep(retry_interval)

    db.add_history(task_id, task.get("name", task_id), biz_type, {
        "status": "fail",
        "fetched": 0, "written": 0, "skipped": 0,
        "duration": 0,
        "message": f"失败（已重试 {retry_times} 次）：{last_error}",
    })
    logger.error("[%s] %s 同步失败: %s", task_id, biz_type, last_error)


def _build_trigger(task) -> object:
    mode = task.get("mode", "daily")
    if mode == "interval":
        seconds = max(int(task.get("interval_seconds", 3600)), 60)
        return IntervalTrigger(seconds=seconds)
    if mode == "multi_daily":
        times = task.get("daily_times") or []
        # 多个每日时间点 = 每个时间点注册一个 CronTrigger，拆成多个 job
        # 这里用一个聚合 trigger 的替代实现：对每个时间点单独 add_job
        # （见 start_task 内的展开逻辑）
        return None
    # daily
    hh, mm = _parse_hhmm(task.get("daily_time", "09:00"))
    return CronTrigger(day_of_week="*", hour=hh, minute=mm)


def _parse_hhmm(s: str):
    try:
        hh, mm = s.split(":")
        return int(hh), int(mm)
    except Exception:
        return 9, 0


def _register_one(task) -> Optional[str]:
    """为单个任务注册调度。返回 job_id；multi_daily 会注册多个 job 并记录到 job_map。"""
    task_id = task["task_id"]
    mode = task.get("mode", "daily")

    if mode == "multi_daily":
        job_ids = []
        for t in task.get("daily_times") or []:
            hh, mm = _parse_hhmm(t)
            job = _scheduler.add_job(
                _run_task,
                CronTrigger(day_of_week="*", hour=hh, minute=mm),
                args=[task_id],
                id=f"{task_id}-{hh:02d}{mm:02d}",
                replace_existing=True,
            )
            job_ids.append(job.id)
        _job_map[task_id] = ",".join(job_ids)
        return _job_map[task_id]

    trigger = _build_trigger(task)
    if trigger is None:
        return None
    job = _scheduler.add_job(
        _run_task, trigger, args=[task_id],
        id=f"task-{task_id}", replace_existing=True,
    )
    _job_map[task_id] = job.id
    return job.id


def load_all_tasks() -> int:
    with _lock:
        count = 0
        for task in db.get_all_tasks():
            if task.get("enabled"):
                _register_one(task)
                count += 1
        if not _scheduler.running:
            _scheduler.start()
        return count


def start_task(task_id: str) -> bool:
    with _lock:
        task = db.get_task(task_id)
        if not task:
            return False
        if not _scheduler.running:
            _scheduler.start()
        _register_one(task)
        db.update_task(task_id, enabled=True)
        logger.info("任务已启动: %s", task_id)
        return True


def stop_task(task_id: str) -> bool:
    with _lock:
        job_key = _job_map.pop(task_id, None)
        if job_key:
            for jid in job_key.split(","):
                try:
                    _scheduler.remove_job(jid)
                except Exception:
                    pass
        db.update_task(task_id, enabled=False)
        logger.info("任务已停止: %s", task_id)
        return True


def trigger_task(task_id: str) -> bool:
    """立即触发一次执行（不改变调度计划）"""
    if not db.get_task(task_id):
        return False
    threading.Thread(target=_run_task, args=(task_id,), daemon=True).start()
    return True


def get_scheduler_status() -> dict:
    jobs = _scheduler.get_jobs()
    return {
        "running": _scheduler.running,
        "job_count": len(jobs),
        "next_run_time": str(jobs[0].next_run_time) if jobs else None,
    }
