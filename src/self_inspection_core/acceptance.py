"""运行基础服务与每日自巡证据模块的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock, MutableClock
from .errors import ConflictError, ValidationError
from .inspection import InspectionService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链与一天的自巡证据流并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "acceptance.sqlite3"
        database = Database(path)
        clock = MutableClock(datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc))  # 当地 09:00
        service = DomainService(database, clock)
        inspection = InspectionService(service)
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
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="enterprise_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="enterprise_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # 每日自巡证据场景：二号场所登记两个涉污车间和一套治污设施。
        service.register_site(request_id="req-site-2", actor_id="operator-001", site_id="site-002",
                              organization_id="org-001", name="二号生产场所", timezone_name="Asia/Shanghai")
        service.record_domain_data(request_id="req-ws-1", actor_id="operator-001", site_id="site-002",
                                   category="workshop_registry", external_key="ws-1",
                                   data={"name": "喷漆车间"})
        service.record_domain_data(request_id="req-ws-2", actor_id="operator-001", site_id="site-002",
                                   category="workshop_registry", external_key="ws-2",
                                   data={"name": "打磨车间"})
        service.record_domain_data(request_id="req-tf-1", actor_id="operator-001", site_id="site-002",
                                   category="treatment_registry", external_key="tf-1",
                                   data={"name": "废气处理设施"})
        checklist = inspection.generate_checklist(request_id="req-checklist", actor_id="operator-001",
                                                  site_id="site-002")

        clock.set(datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc))  # 当地 10:00
        ev1 = inspection.submit_evidence(request_id="req-ev-1", actor_id="operator-001", site_id="site-002",
                                         item_key="workshop:ws-1", evidence_hash="1" * 64,
                                         captured_at="2026-09-25T09:30:00+08:00",
                                         storage_ref="oss://evidence/ws1-0930.jpg")
        ev1_replay = inspection.submit_evidence(request_id="req-ev-1", actor_id="operator-001", site_id="site-002",
                                                item_key="workshop:ws-1", evidence_hash="1" * 64,
                                                captured_at="2026-09-25T09:30:00+08:00",
                                                storage_ref="oss://evidence/ws1-0930.jpg")
        ev1_dup = inspection.submit_evidence(request_id="req-ev-1-dup", actor_id="operator-001", site_id="site-002",
                                             item_key="workshop:ws-1", evidence_hash="1" * 64,
                                             captured_at="2026-09-25T01:30:00Z",
                                             storage_ref="oss://evidence/ws1-0930.jpg")
        inspection.submit_evidence(request_id="req-ev-2a", actor_id="operator-001", site_id="site-002",
                                   item_key="workshop:ws-2", evidence_hash="2" * 64,
                                   captured_at="2026-09-25T09:40:00+08:00",
                                   storage_ref="oss://evidence/ws2-0940.jpg")
        ev2b = inspection.submit_evidence(request_id="req-ev-2b", actor_id="operator-001", site_id="site-002",
                                          item_key="workshop:ws-2", evidence_hash="3" * 64,
                                          captured_at="2026-09-25T09:50:00+08:00",
                                          storage_ref="oss://evidence/ws2-0950.jpg")
        correction = inspection.submit_correction(request_id="req-cor-1", actor_id="operator-001",
                                                  site_id="site-002", local_date="2026-09-25",
                                                  item_key="workshop:ws-2",
                                                  content="第一版拍摄模糊，以第二版为准")
        dispute = inspection.record_decision(request_id="req-dec-1", actor_id="reviewer-001",
                                             site_id="site-002", local_date="2026-09-25",
                                             item_key="workshop:ws-2", action="dispute",
                                             note="同一项目两版凭据不一致")
        dispute_report = inspection.daily_report(site_id="site-002", local_date="2026-09-25")
        accept = inspection.record_decision(request_id="req-dec-2", actor_id="reviewer-001",
                                            site_id="site-002", local_date="2026-09-25",
                                            item_key="workshop:ws-2", action="accept",
                                            evidence_id=ev2b["evidence_id"], note="采信第二版")
        supplement = inspection.record_decision(request_id="req-dec-3", actor_id="reviewer-001",
                                                site_id="site-002", local_date="2026-09-25",
                                                item_key="treatment:tf-1", action="request_supplement",
                                                note="请补充设施运行台账照片")
        skew_rejected = False
        try:
            inspection.submit_evidence(request_id="req-ev-skew", actor_id="operator-001", site_id="site-002",
                                       item_key="workshop:ws-1", evidence_hash="4" * 64,
                                       captured_at="2026-09-25T11:30:00+08:00",
                                       storage_ref="oss://evidence/skew.jpg")
        except ValidationError:
            skew_rejected = True

        clock.set(datetime(2026, 9, 25, 17, 30, tzinfo=timezone.utc))  # 次日 01:30，跨午夜
        overdue_report = inspection.daily_report(site_id="site-002", local_date="2026-09-25")
        late_correction_rejected = False
        try:
            inspection.submit_correction(request_id="req-cor-late", actor_id="operator-001",
                                         site_id="site-002", local_date="2026-09-25",
                                         item_key="workshop:ws-1", content="超时补充说明")
        except ConflictError:
            late_correction_rejected = True
        ev3 = inspection.submit_evidence(request_id="req-ev-3", actor_id="operator-001", site_id="site-002",
                                         item_key="treatment:tf-1", evidence_hash="5" * 64,
                                         captured_at="2026-09-25T23:40:00+08:00",
                                         storage_ref="oss://evidence/tf1-2340.jpg")
        final_report = inspection.daily_report(site_id="site-002", local_date="2026-09-25")
        timeline = inspection.daily_timeline(site_id="site-002", local_date="2026-09-25")
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        database.close()

        # 模拟服务重启：同一数据库文件重建服务，结论必须一致。
        reopened = Database(path)
        restarted_service = DomainService(reopened, FixedClock(datetime(2026, 9, 25, 17, 30,
                                                                      tzinfo=timezone.utc)))
        restarted_inspection = InspectionService(restarted_service)
        restarted_report = restarted_inspection.daily_report(site_id="site-002", local_date="2026-09-25")
        restarted_valid, _ = restarted_service.verify_audit()
        restarted_timeline = restarted_inspection.daily_timeline(site_id="site-002", local_date="2026-09-25")
        reopened.close()
        restart_consistent = (restarted_report == final_report and restarted_valid
                              and restarted_timeline["events"] == timeline["events"])

        ws2 = next(item for item in final_report["items"] if item["item_key"] == "workshop:ws-2")
        inspection_checks = {
            "checklist_version": checklist["version"],
            "checklist_items": checklist["item_count"],
            "evidence_replayed": ev1_replay["replayed"] and ev1_replay["evidence_id"] == ev1["evidence_id"],
            "evidence_deduplicated": ev1_dup["deduplicated"] and ev1_dup["evidence_id"] == ev1["evidence_id"],
            "conflict_versions_kept": ev2b["version"] == 2 and ws2["version_count"] == 2,
            "correction_before_deadline": correction["checklist_version"] == 1,
            "dispute_visible": dispute_report["summary"]["disputed_items"] == ["workshop:ws-2"],
            "decision_references_checklist": dispute["checklist_version"] == 1
            and accept["checklist_version"] == 1 and supplement["checklist_version"] == 1,
            "accepted_version": ws2["resolution_state"] == "accepted"
            and ws2["accepted_evidence_id"] == ev2b["evidence_id"],
            "supplement_requested": supplement["action"] == "request_supplement",
            "clock_skew_rejected": skew_rejected,
            "overdue_status": overdue_report["status"] == "overdue",
            "overdue_reasons": sorted({reason["reason"] for reason
                                       in overdue_report["summary"]["overdue_reasons"]}),
            "late_correction_rejected": late_correction_rejected,
            "late_evidence_flagged": ev3["late"] and ev3["local_date"] == "2026-09-25",
            "final_status": final_report["status"],
            "late_items": final_report["summary"]["late_items"],
            "disputed_items": final_report["summary"]["disputed_items"],
            "timeline_events": len(timeline["events"]),
            "timeline_valid": timeline["audit_valid"],
            "restart_consistent": restart_consistent,
        }
        boolean_checks = [value for value in inspection_checks.values() if isinstance(value, bool)]
        ok = (valid and all(boolean_checks)
              and inspection_checks["checklist_version"] == 1
              and inspection_checks["checklist_items"] == 3
              and inspection_checks["overdue_status"]
              and inspection_checks["overdue_reasons"] == ["missing_evidence"]
              and inspection_checks["final_status"] == "complete"
              and inspection_checks["late_items"] == ["treatment:tf-1"]
              and inspection_checks["disputed_items"] == []
              and inspection_checks["timeline_events"] == 9)
        return {"status": "ok" if ok else "failed",
                "records": len(records),
                "audit_events": event_count,
                "audit_valid": valid,
                "first_replayed": first.replayed,
                "second_replayed": replay.replayed,
                "inspection": inspection_checks}


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
