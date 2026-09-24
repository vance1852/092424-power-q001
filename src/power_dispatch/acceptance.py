"""贯通电价、送出线路、燃料库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96", "94"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部电厂", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "PEAK_VALLEY", "quantity_mwh": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_mwh": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")

    def verify(label: str, condition: bool) -> None:
        if not condition:
            raise AssertionError(f"电价连续段离线验收失败：{label}")

    price_full = service.price_summary("PEAK_VALLEY")
    streak_full = price_full["latest_streak"]
    # 七个报价点的六连跌：次数是相邻结算日之间的六次变化，不是七个报价点。
    verify("完整窗口方向为下跌", streak_full["direction"] == "down")
    verify("七连报价点计为六次变化", streak_full["changes"] == 6)
    verify("起始日锚定首条下跌边的基准日", streak_full["start_date"] == "2026-09-18")
    verify("起始报价为 108", streak_full["start_close"] == "108")
    verify("截止报价为 94", streak_full["end_close"] == "94")
    verify("起始版本依据为 rev-18", streak_full["start_revision"] == "rev-18")
    verify("截止版本依据为 rev-24", streak_full["end_revision"] == "rev-24")
    verify("完整窗口未被截断", streak_full["truncated"] is False)

    # 同日修订：登记新来源版本后，连续段按修订后的报价重算并更新版本依据。
    service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": "2026-09-24", "close_cny": "94.5", "source_revision": "rev-24-corrected", "observed_at": "2026-09-24T22:30:00Z"})
    price_revised = service.price_summary("PEAK_VALLEY")
    streak_revised = price_revised["latest_streak"]
    verify("修订后仍为六次变化", streak_revised["changes"] == 6)
    verify("修订后采用新报价", streak_revised["end_close"] == "94.5")
    verify("修订后累计跌幅重算", streak_revised["percent_change"] == "-12.5000")
    verify("修订后版本依据更新", streak_revised["end_revision"] == "rev-24-corrected")
    verify("最新报价携带修订版本", price_revised["latest"]["source_revision"] == "rev-24-corrected")

    # 查询窗口截断：只取最近三个结算日，连续下跌在窗口起点之前仍在延续。
    price_window = service.price_summary("PEAK_VALLEY", sessions=3)
    streak_window = price_window["latest_streak"]
    verify("截断窗口内为两次变化", streak_window["changes"] == 2)
    verify("截断窗口起始日为 2026-09-22", streak_window["start_date"] == "2026-09-22")
    verify("截断窗口标记被截断", streak_window["truncated"] is True)

    # 不足两个结算日：没有相邻结算日，无法形成变化，连续段为空而不是伪造一次平盘。
    price_single = service.price_summary("PEAK_VALLEY", sessions=1)
    verify("单点窗口观测数为一", price_single["observations"] == 1)
    verify("单点窗口不产生连续段", price_single["latest_streak"] is None)

    price_checks = {
        "full_window": price_full,
        "after_revision": price_revised,
        "truncated_window": price_window,
        "single_point": price_single,
    }
    result = {"status": "ok", "price": price_revised, "price_checks": price_checks, "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行电厂调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
