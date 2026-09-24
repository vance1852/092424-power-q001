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
    # 七个报价点之间应有六次下跌；随后对末日做同日修订，连跌次数不变而累计跌幅与版本依据重算。
    revised = service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": "2026-09-24", "close_cny": "93", "source_revision": "rev-24-corrected", "observed_at": "2026-09-24T22:00:00Z"})
    full = service.price_summary("PEAK_VALLEY")
    full_streak = full["latest_streak"]
    assert full["observations"] == 7, full
    assert full_streak["direction"] == "down" and full_streak["sessions"] == 6, full_streak
    assert (full_streak["start_date"], full_streak["start_close"], full_streak["start_source_revision"]) == ("2026-09-18", "108", "rev-18"), full_streak
    assert (full_streak["end_date"], full_streak["end_close"], full_streak["end_quote_id"]) == ("2026-09-24", "93", revised["quote_id"]), full_streak
    assert full_streak["percent_change"] == "-13.8889", full_streak
    assert full["window"]["earlier_history"] is False, full["window"]
    # 查询窗口截断：只取五个报价点时，连跌只能是窗口内的四次，且窗口之前存在更早报价。
    window = service.price_summary("PEAK_VALLEY", sessions=5)
    window_streak = window["latest_streak"]
    assert window["observations"] == 5 and window_streak["sessions"] == 4, window
    assert (window_streak["start_date"], window_streak["end_date"]) == ("2026-09-20", "2026-09-24"), window_streak
    assert window["window"]["earlier_history"] is True and window["window"]["requested_sessions"] == 5, window["window"]
    # 不足两个报价点的边界：唯一报价不存在相邻变化，连跌判定为 None。
    single = service.price_summary("PEAK_VALLEY", sessions=1)
    assert single["observations"] == 1 and single["latest_streak"] is None, single
    assert single["window"]["start_date"] == single["window"]["end_date"] == "2026-09-24", single["window"]
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
    result = {"status": "ok", "price": full, "streak_checks": {"six_consecutive_drops": full_streak, "revised_quote_id": revised["quote_id"], "truncated_window": window, "single_point": single}, "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "audit": service.audit_chain("audit"), "workspace": workspace.name}
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
