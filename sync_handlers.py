"""
业务线注册表 + 内置示例（模拟数据源）
========================================
设计目标：新增一条业务线只需注册 4 个东西，调度器与管理端零改动：
  fetcher  : 从数据源拉取原始单据（真实环境 = ERP Open API 封装）
  mapper   : 字段口径映射 / 扁平化（业务字段 -> 目标表字段）
  writer   : 写入目标表（真实环境 = 飞书多维表 API），支持幂等去重
  row_key  : 幂等去重唯一键（如单据号）

Demo 内置两条示例业务线（采购订单 / 财务应付单），
数据由 mock 生成，用于演示同一条工程链路。
"""
import random
import datetime
from typing import Callable, Dict, List, Optional, Tuple

# 飞书多维表写入的"行"示意结构
# 真实实现里 writer 会调用 lark-oapi 的 bitable 批量接口

_REGISTRY: Dict[str, Dict[str, Callable]] = {}


def register_handler(biz_type: str, fetcher, mapper, writer, row_key: str) -> None:
    """注册一条业务线。"""
    _REGISTRY[biz_type] = {
        "biz_type": biz_type,
        "fetcher": fetcher,
        "mapper": mapper,
        "writer": writer,
        "row_key": row_key,
    }


def get_handler(biz_type: str) -> Optional[Dict[str, Callable]]:
    return _REGISTRY.get(biz_type)


def registered_biz_types() -> List[str]:
    return list(_REGISTRY.keys())


def probe_erp_connection() -> dict:
    """ERP 连通性探测（Demo：模拟成功；真实环境替换为获取 token 的调用）"""
    return {"connected": True, "message": "模拟连接成功（请在真实环境接入 ERP Open API）"}


def probe_feishu_connection(app_token: str = "", table_id: str = "") -> dict:
    """飞书连通性探测（Demo：模拟成功）"""
    return {"connected": True, "message": "模拟连接成功（请在真实环境接入飞书多维表 API）"}


# ==================== 内置示例 1：采购订单 ====================

def _mock_fetch_purchase_orders(start_date: str, end_date: str, verify: bool = True):
    """模拟从 ERP 分页拉取采购订单（真实环境为 Open API 分页 + OAuth Token 自动刷新）"""
    start = datetime.date.fromisoformat(start_date)
    end = datetime.date.fromisoformat(end_date)
    days = max((end - start).days + 1, 1)

    sample_skus = ["SKU-1001", "SKU-1002", "SKU-1003", "SKU-1004", "SKU-1005"]
    sample_suppliers = ["供应商A", "供应商B", "供应商C"]
    sample_currencies = ["CNY", "USD", "THB"]

    rows = []
    for i in range(days * 3 + random.randint(1, 6)):
        bill_date = start + datetime.timedelta(days=i // 3 % days)
        rows.append({
            "bill_no": f"PO-{bill_date:%Y%m%d}-{i:03d}",
            "bill_date": bill_date.strftime("%Y-%m-%d"),
            "supplier": random.choice(sample_suppliers),
            "currency": random.choice(sample_currencies),
            "sku": random.choice(sample_skus),
            "qty": random.randint(1, 500),
            "price": round(random.uniform(5, 2000), 2),
            "amount_tax": round(random.uniform(1000, 50000), 2),
        })
    return rows


def _map_purchase_order(raw: List[dict]) -> List[dict]:
    """字段口径映射：业务字段 -> 目标表字段（含金额口径统一）"""
    mapped = []
    for r in raw:
        mapped.append({
            "单据编号": r["bill_no"],
            "单据日期": r["bill_date"],
            "供应商": r["supplier"],
            "币别": r["currency"],
            "SKU": r["sku"],
            "数量": r["qty"],
            "单价": r["price"],
            "价税合计": round(r["amount_tax"], 2),
        })
    return mapped


def _mock_write_feishu(rows: List[dict], row_key: str) -> Tuple[int, int]:
    """模拟写入飞书多维表（真实环境为 lark-oapi bitable 批量写入 + 幂等去重）

    返回 (written, skipped)。Demo 按 row_key 去重模拟幂等。
    """
    seen = set()
    written = 0
    for row in rows:
        key = row.get(row_key) or row.get("单据编号") or row.get("bill_no")
        if key in seen:
            continue
        seen.add(key)
        written += 1
    return written, len(rows) - written


# ==================== 内置示例 2：财务应付单 ====================

def _mock_fetch_payables(start_date: str, end_date: str, verify: bool = True):
    start = datetime.date.fromisoformat(start_date)
    end = datetime.date.fromisoformat(end_date)
    days = max((end - start).days + 1, 1)
    rows = []
    for i in range(days * 2 + random.randint(1, 4)):
        bill_date = start + datetime.timedelta(days=i % days)
        rows.append({
            "ap_no": f"AP-{bill_date:%Y%m%d}-{i:03d}",
            "bill_date": bill_date.strftime("%Y-%m-%d"),
            "vendor": random.choice(["供应商A", "供应商B"]),
            "biz_type": "采购应付",
            "payable_amount": round(random.uniform(2000, 80000), 2),
            "paid_amount": round(random.uniform(0, 40000), 2),
        })
    return rows


def _map_payable(raw: List[dict]) -> List[dict]:
    return [
        {
            "应付单号": r["ap_no"],
            "单据日期": r["bill_date"],
            "往来单位": r["vendor"],
            "业务类型": r["biz_type"],
            "应付金额": r["payable_amount"],
            "已付金额": r["paid_amount"],
            "未付金额": round(r["payable_amount"] - r["paid_amount"], 2),
        }
        for r in raw
    ]


# ==================== 注册内置业务线 ====================

register_handler(
    biz_type="采购订单",
    fetcher=_mock_fetch_purchase_orders,
    mapper=_map_purchase_order,
    writer=_mock_write_feishu,
    row_key="bill_no",
)

register_handler(
    biz_type="财务应付单",
    fetcher=_mock_fetch_payables,
    mapper=_map_payable,
    writer=_mock_write_feishu,
    row_key="ap_no",
)
