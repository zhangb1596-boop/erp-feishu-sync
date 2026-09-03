"""
数据层：JSON 文件持久化
职责：任务 / 同步目标 / 执行历史 / 操作日志 的读写
说明：生产环境可平替为 SQLite / 数据库，接口保持一致。
"""
import os
import json
import time
import threading
from typing import Optional, List, Dict, Any

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
TARGETS_FILE = os.path.join(DATA_DIR, "sync_targets.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
OPLOG_FILE = os.path.join(DATA_DIR, "operation_log.json")

_lock = threading.Lock()


# ---------------- 通用 IO ----------------

def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------- 任务 ----------------

def save_task(task_id: str, name: str, task_type: str, biz_type: str, mode: str,
              interval_seconds: int, daily_times: list, daily_time: str,
              start_date: str, end_date: str, use_today_as_end_date: bool,
              verify: bool, retry_times: int, retry_interval: int,
              sync_target_id: Optional[str], enabled: bool) -> None:
    with _lock:
        tasks = _read_json(TASKS_FILE, [])
        task = {
            "task_id": task_id, "name": name, "task_type": task_type,
            "biz_type": biz_type, "mode": mode,
            "interval_seconds": interval_seconds,
            "daily_times": daily_times, "daily_time": daily_time,
            "start_date": start_date, "end_date": end_date,
            "use_today_as_end_date": use_today_as_end_date,
            "verify": verify, "retry_times": retry_times,
            "retry_interval": retry_interval,
            "sync_target_id": sync_target_id, "enabled": enabled,
        }
        # 存在则覆盖
        tasks = [t for t in tasks if t.get("task_id") != task_id]
        tasks.append(task)
        _write_json(TASKS_FILE, tasks)


def get_all_tasks() -> List[Dict[str, Any]]:
    return _read_json(TASKS_FILE, [])


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    for t in get_all_tasks():
        if t.get("task_id") == task_id:
            return t
    return None


def update_task(task_id: str, **fields) -> Optional[Dict[str, Any]]:
    with _lock:
        tasks = _read_json(TASKS_FILE, [])
        for t in tasks:
            if t.get("task_id") == task_id:
                t.update(fields)
                _write_json(TASKS_FILE, tasks)
                return t
    return None


def delete_task(task_id: str) -> bool:
    with _lock:
        tasks = _read_json(TASKS_FILE, [])
        rest = [t for t in tasks if t.get("task_id") != task_id]
        if len(rest) == len(tasks):
            return False
        _write_json(TASKS_FILE, rest)
        return True


# ---------------- 同步目标 ----------------

def save_sync_target(target_id: str, name: str, biz_type: str,
                     app_token: str, table_id: str,
                     auto_create_fields: bool = True) -> Dict[str, Any]:
    with _lock:
        targets = _read_json(TARGETS_FILE, [])
        rec = {
            "target_id": target_id, "name": name, "biz_type": biz_type,
            "app_token": app_token, "table_id": table_id,
            "auto_create_fields": auto_create_fields,
            "updated_at": int(time.time()),
        }
        targets = [t for t in targets if t.get("target_id") != target_id]
        targets.append(rec)
        _write_json(TARGETS_FILE, targets)
        return rec


def get_all_sync_targets(biz_type: Optional[str] = None) -> List[Dict[str, Any]]:
    targets = _read_json(TARGETS_FILE, [])
    if biz_type:
        targets = [t for t in targets if t.get("biz_type") == biz_type]
    return targets


def get_sync_target(target_id: str) -> Optional[Dict[str, Any]]:
    for t in get_all_sync_targets():
        if t.get("target_id") == target_id:
            return t
    return None


def delete_sync_target(target_id: str) -> bool:
    with _lock:
        targets = _read_json(TARGETS_FILE, [])
        rest = [t for t in targets if t.get("target_id") != target_id]
        if len(rest) == len(targets):
            return False
        _write_json(TARGETS_FILE, rest)
        return True


# ---------------- 执行历史 ----------------

def add_history(task_id: str, task_name: str, biz_type: str, result: Dict[str, Any]) -> None:
    with _lock:
        history = _read_json(HISTORY_FILE, [])
        history.append({
            "time": int(time.time()),
            "task_id": task_id,
            "task_name": task_name,
            "biz_type": biz_type,
            "status": result.get("status", "unknown"),
            "fetched": result.get("fetched", 0),
            "written": result.get("written", 0),
            "skipped": result.get("skipped", 0),
            "duration": result.get("duration", 0),
            "message": result.get("message", ""),
        })
        # 只保留最近 2000 条
        history = history[-2000:]
        _write_json(HISTORY_FILE, history)


def get_history(task_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    history = _read_json(HISTORY_FILE, [])
    rows = [h for h in history if h.get("task_id") == task_id]
    return rows[-limit:][::-1]


def get_all_history(limit: int = 100) -> List[Dict[str, Any]]:
    history = _read_json(HISTORY_FILE, [])
    return history[-limit:][::-1]


def get_history_stats(days: int = 7) -> Dict[str, Any]:
    """统计最近 N 天执行成功率 / 总行数"""
    history = _read_json(HISTORY_FILE, [])
    since = int(time.time()) - days * 86400
    rows = [h for h in history if h.get("time", 0) >= since]
    total = len(rows)
    success = sum(1 for h in rows if h.get("status") == "success")
    fetched = sum(h.get("fetched", 0) for h in rows)
    written = sum(h.get("written", 0) for h in rows)
    return {
        "days": days,
        "total_runs": total,
        "success_runs": success,
        "success_rate": round(success / total * 100, 1) if total else 0.0,
        "total_fetched": fetched,
        "total_written": written,
    }


def clear_history(task_id: Optional[str] = None) -> int:
    with _lock:
        history = _read_json(HISTORY_FILE, [])
        if task_id:
            rest = [h for h in history if h.get("task_id") != task_id]
        else:
            rest = []
        _write_json(HISTORY_FILE, rest)
        return len(history) - len(rest)


# ---------------- 操作日志 ----------------

def add_operation_log(action: str, target: str, detail: str) -> None:
    with _lock:
        logs = _read_json(OPLOG_FILE, [])
        logs.append({
            "time": int(time.time()),
            "action": action,
            "target": target,
            "detail": detail,
        })
        logs = logs[-2000:]
        _write_json(OPLOG_FILE, logs)


def get_operation_log(limit: int = 100) -> List[Dict[str, Any]]:
    logs = _read_json(OPLOG_FILE, [])
    return logs[-limit:][::-1]
