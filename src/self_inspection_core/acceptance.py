"""运行基础服务与每日自巡证据模块的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import StepClock
from .evidence import EvidenceService
from .storage import Database

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def _bootstrap(service: EvidenceService) -> None:
    service.register_organization(request_id="req-org", actor_id="bootstrap",
                                  organization_id="org-001", name="示范企业")
    service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                           display_name="系统管理员", role="admin", organization_id="org-001")
    service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                           display_name="环保负责人", role="operator", organization_id="org-001")
    service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                           display_name="监管人员", role="reviewer", organization_id="org-001")
    service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                          organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")


def _first_day(service: EvidenceService) -> dict[str, object]:
    """完整的第一天：幂等重试、内容去重、冲突版本、纠正、争议与采信。"""

    first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                       category="enterprise_profile", external_key="record-001",
                                       data={"name": "基础资料", "enabled": True})
    replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                        category="enterprise_profile", external_key="record-001",
                                        data={"name": "基础资料", "enabled": True})
    service.record_domain_data(request_id="req-workshop", actor_id="operator-001", site_id="site-001",
                               category="workshop_registry", external_key="wh-spray",
                               data={"name": "喷漆车间", "enabled": True})
    service.record_domain_data(request_id="req-treatment", actor_id="operator-001", site_id="site-001",
                               category="treatment_registry", external_key="tr-vocs",
                               data={"name": "废气处理设施", "enabled": True})
    plan = service.generate_plan(request_id="req-plan-1", actor_id="operator-001",
                                 site_id="site-001", local_date="2026-09-25")
    workshop_item = "workshop_sealed:wh-spray"
    treatment_item = "treatment_running:tr-vocs"
    evidence = service.submit_evidence(request_id="req-ev-1", actor_id="operator-001",
                                       plan_id=plan["plan_id"], item_key=workshop_item,
                                       evidence_hash=HASH_A, captured_at="2026-09-25T08:30:00+08:00",
                                       storage_ref="device://cam-01/20260925-0830.jpg")
    evidence_replay = service.submit_evidence(request_id="req-ev-1", actor_id="operator-001",
                                              plan_id=plan["plan_id"], item_key=workshop_item,
                                              evidence_hash=HASH_A, captured_at="2026-09-25T08:30:00+08:00",
                                              storage_ref="device://cam-01/20260925-0830.jpg")
    evidence_dupe = service.submit_evidence(request_id="req-ev-1b", actor_id="operator-001",
                                            plan_id=plan["plan_id"], item_key=workshop_item,
                                            evidence_hash=HASH_A, captured_at="2026-09-25T08:30:00+08:00",
                                            storage_ref="device://cam-01/20260925-0830.jpg")
    treatment_v1 = service.submit_evidence(request_id="req-ev-2", actor_id="operator-001",
                                           plan_id=plan["plan_id"], item_key=treatment_item,
                                           evidence_hash=HASH_B, captured_at="2026-09-25T08:40:00+08:00",
                                           storage_ref="device://cam-02/20260925-0840.jpg")
    treatment_v2 = service.submit_evidence(request_id="req-ev-3", actor_id="operator-001",
                                           plan_id=plan["plan_id"], item_key=treatment_item,
                                           evidence_hash=HASH_C, captured_at="2026-09-25T08:45:00+08:00",
                                           storage_ref="device://cam-02/20260925-0845.jpg")
    service.submit_correction(request_id="req-fix-1", actor_id="operator-001",
                              evidence_id=treatment_v2["evidence_id"],
                              note="第二段视频为补拍角度，原始记录未删除")
    dispute = service.raise_dispute(request_id="req-dispute-1", actor_id="reviewer-001",
                                    evidence_id=treatment_v2["evidence_id"], reason="补拍画面无法确认设施运行")
    decision = service.decide_evidence(request_id="req-decision-1", actor_id="reviewer-001",
                                       plan_id=plan["plan_id"], item_key=treatment_item,
                                       decision="accept_version", evidence_id=treatment_v1["evidence_id"],
                                       note="采信首次提交版本")
    report = service.day_report(site_id="site-001", local_date="2026-09-25")
    return {"plan": plan, "report": report, "first_replayed": first.replayed,
            "second_replayed": replay.replayed, "evidence": evidence,
            "evidence_replay": evidence_replay, "evidence_dupe": evidence_dupe,
            "treatment_v2": treatment_v2, "dispute": dispute, "decision": decision}


def _second_day(service: EvidenceService, clock: StepClock) -> dict[str, object]:
    """第二天：跨午夜补传被标记逾期，争议保持未决。"""

    clock.set(datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc))
    plan = service.generate_plan(request_id="req-plan-2", actor_id="operator-001",
                                 site_id="site-001", local_date="2026-09-26")
    clock.set(datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc))
    late = service.submit_evidence(request_id="req-ev-4", actor_id="operator-001",
                                   plan_id=plan["plan_id"], item_key="workshop_sealed:wh-spray",
                                   evidence_hash=HASH_D, captured_at="2026-09-26T23:30:00+08:00",
                                   storage_ref="device://cam-01/20260926-2330.jpg")
    service.raise_dispute(request_id="req-dispute-2", actor_id="reviewer-001",
                          evidence_id=late["evidence_id"], reason="逾期补传需现场复核")
    report = service.day_report(site_id="site-001", local_date="2026-09-26")
    return {"plan": plan, "report": report, "late": late}


def run() -> dict[str, object]:
    """执行完整自巡链并核对重启前后结论一致。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "acceptance.sqlite3"
        clock = StepClock(datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc))
        database = Database(path)
        service = EvidenceService(database, clock)
        _bootstrap(service)
        day_one = _first_day(service)
        day_two = _second_day(service, clock)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        timeline = service.plan_timeline(day_two["plan"]["plan_id"])
        day_one_final = service.day_report(site_id="site-001", local_date="2026-09-25")
        database.close()

        restarted = EvidenceService(Database(path), clock)
        report_one_after = restarted.day_report(site_id="site-001", local_date="2026-09-25")
        report_two_after = restarted.day_report(site_id="site-001", local_date="2026-09-26")
        valid_after, _ = restarted.verify_audit()
        restarted.database.close()

        restart_consistent = (report_one_after == day_one_final
                              and report_two_after == day_two["report"] and valid_after)
        checks = {
            "audit_valid": valid and valid_after,
            "day_one_complete": day_one["report"]["day_status"] == "complete",
            "day_one_dispute_resolved": not day_one["report"]["disputed_evidence"],
            "replay_returned_original": (day_one["evidence_replay"]["replayed"]
                                         and day_one["evidence_replay"]["evidence_id"]
                                         == day_one["evidence"]["evidence_id"]),
            "duplicate_deduped": (day_one["evidence_dupe"]["deduplicated"]
                                  and day_one["evidence_dupe"]["evidence_id"]
                                  == day_one["evidence"]["evidence_id"]),
            "conflict_kept": day_one["treatment_v2"]["version_no"] == 2,
            "decision_resolved_dispute": (day_one["dispute"]["dispute_id"]
                                          in day_one["decision"]["resolved_disputes"]),
            "day_two_overdue": day_two["report"]["day_status"] == "overdue",
            "day_two_reasons": {item["reason"] for item in day_two["report"]["outstanding_items"]} == {"missing", "late"},
            "day_two_late_flag": day_two["late"]["late"],
            "day_two_disputed": len(day_two["report"]["disputed_evidence"]) == 1,
            "restart_consistent": restart_consistent,
        }
        return {"status": "ok" if all(checks.values()) else "failed", "checks": checks,
                "records": len(records), "audit_events": event_count, "audit_valid": valid,
                "first_replayed": day_one["first_replayed"], "second_replayed": day_one["second_replayed"],
                "day_one_status": day_one["report"]["day_status"],
                "day_two_status": day_two["report"]["day_status"],
                "day_two_outstanding": day_two["report"]["outstanding_items"],
                "day_two_disputed": len(day_two["report"]["disputed_evidence"]),
                "timeline_events": len(timeline)}


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
