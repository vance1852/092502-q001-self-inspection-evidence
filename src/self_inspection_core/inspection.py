"""每日自巡证据模块：清单生成、凭据版本、纠正说明、监管决定与日结报告。

业务规则：
- 应巡清单按场所时区的当地日期生成，来源于 workshop_registry 与 treatment_registry
  领域资料；资料指纹变化时产生新的清单版本，旧版本保留，监管决定引用当时的清单版本。
- 凭据按 (场所, 当地日期, 应巡项目) 形成只增不改的版本链：request_id 完全相同的重试
  返回原回执，内容完全相同的重复提交去重，不同内容保留为新版本而不覆盖。
- 凭据归属日期由 captured_at 按场所时区推导；设备时钟偏差超过 300 秒被拒绝；
  接收时间晚于当地日期截止时刻（次日 00:00）的凭据标记为补传 late。
- 企业可在截止前提交纠正说明；监管人员可发起争议、采信某个版本或要求补证；
  所有动作写入哈希链审计，形成不可改写的时间线。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .audit import append_event, digest, verify_chain
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .service import DomainService


HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
CLOCK_SKEW_TOLERANCE = timedelta(seconds=300)

# 领域资料类别 -> (项目类型, item_key 前缀, 巡核要求)
ITEM_SOURCES = {
    "workshop_registry": ("workshop_sealing", "workshop", "涉污车间保持密闭并上传佐证"),
    "treatment_registry": ("treatment_operation", "treatment", "治污设施保持运行并上传佐证"),
}

DECISION_ACTIONS = frozenset({"dispute", "accept", "request_supplement"})
RESOLUTION_STATES = {
    "dispute": "disputed",
    "accept": "accepted",
    "request_supplement": "supplement_requested",
}


class InspectionService:
    """在基础领域服务之上实现每日自巡证据规则。"""

    def __init__(self, domain: DomainService) -> None:
        self.domain = domain
        self.database = domain.database

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.domain.clock.now().astimezone(timezone.utc)

    def _now_text(self) -> str:
        return self._now().isoformat().replace("+00:00", "Z")

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _zone(self, site) -> ZoneInfo:
        try:
            return ZoneInfo(site["timezone_name"])
        except Exception as exc:
            raise ValidationError("场所时区无效") from exc

    def _parse_local_date(self, value: Any) -> date:
        try:
            return date.fromisoformat(str(value).strip())
        except ValueError as exc:
            raise ValidationError("local_date 必须是 YYYY-MM-DD 格式") from exc

    def _parse_moment(self, value: Any, field: str) -> datetime:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field} 不是有效的 ISO 8601 时间") from exc
        if moment.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区偏移")
        return moment.astimezone(timezone.utc)

    def _moment_text(self, moment: datetime) -> str:
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _deadline(self, zone: ZoneInfo, local_date: date) -> datetime:
        """当地日期次日 00:00（场所时区）对应的 UTC 时刻。"""

        return datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)

    def _check_scope(self, actor, site) -> None:
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")

    def _execute(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                 create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        """复用基础幂等机制，并在重放时还原首次响应。"""

        holder: dict[str, Any] = {}

        def wrapped() -> tuple[str, str, dict[str, Any]]:
            resource_type, resource_id, response = create()
            holder["response"] = response
            return resource_type, resource_id, response

        receipt = self.domain._idempotent(connection, request_id=request_id,
                                          action=action, payload=payload, create=wrapped)
        if receipt.replayed:
            row = connection.execute(
                "SELECT response_json FROM request_receipts WHERE request_id=?", (receipt.request_id,)
            ).fetchone()
            response = json.loads(row["response_json"])
        else:
            response = holder["response"]
        response["replayed"] = receipt.replayed
        return response

    # ------------------------------------------------------------------
    # 清单生成
    # ------------------------------------------------------------------

    def _registry_items(self, connection, site_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM domain_records WHERE site_id=? AND category IN (?, ?) ORDER BY external_key",
            (site_id, "workshop_registry", "treatment_registry"),
        ).fetchall()
        items = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("enabled", True) is False:
                continue
            item_type, prefix, requirement = ITEM_SOURCES[row["category"]]
            items.append({
                "item_key": f"{prefix}:{row['external_key']}",
                "item_type": item_type,
                "title": str(payload.get("name") or row["external_key"]),
                "requirement": requirement,
                "source_record_id": row["record_id"],
                "payload_hash": row["payload_hash"],
            })
        items.sort(key=lambda item: item["item_key"])
        return items

    def _fingerprint(self, items: list[dict[str, Any]]) -> str:
        material = [{key: item[key] for key in
                     ("item_key", "item_type", "title", "source_record_id", "payload_hash")}
                    for item in items]
        return digest(material)

    def _current_checklist(self, connection, site_id: str, local_date: date):
        return connection.execute(
            "SELECT * FROM inspection_checklists WHERE site_id=? AND local_date=? "
            "ORDER BY version DESC LIMIT 1",
            (site_id, local_date.isoformat()),
        ).fetchone()

    def _ensure_checklist(self, connection, *, site, local_date: date, actor_id: str):
        """返回当日有效清单；资料指纹变化时生成新版本，旧版本保留。"""

        current = self._current_checklist(connection, site["site_id"], local_date)
        items = self._registry_items(connection, site["site_id"])
        fingerprint = self._fingerprint(items)
        if current is not None and current["source_fingerprint"] == fingerprint:
            return current
        version = 1 if current is None else current["version"] + 1
        checklist_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO inspection_checklists(checklist_id,site_id,local_date,version,"
            "source_fingerprint,generated_by,generated_at) VALUES(?,?,?,?,?,?,?)",
            (checklist_id, site["site_id"], local_date.isoformat(), version,
             fingerprint, actor_id, self._now_text()),
        )
        for position, item in enumerate(items, start=1):
            connection.execute(
                "INSERT INTO inspection_items(item_id,checklist_id,item_key,item_type,title,"
                "requirement,source_record_id,position) VALUES(?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, checklist_id, item["item_key"], item["item_type"],
                 item["title"], item["requirement"], item["source_record_id"], position),
            )
        append_event(connection, actor_id=actor_id, action="inspection.checklist_generated",
                     resource_type="inspection_checklist", resource_id=checklist_id,
                     detail={"site_id": site["site_id"], "local_date": local_date.isoformat(),
                             "version": version, "item_count": len(items),
                             "source_fingerprint": fingerprint},
                     occurred_at=self._now_text())
        return connection.execute(
            "SELECT * FROM inspection_checklists WHERE checklist_id=?", (checklist_id,)
        ).fetchone()

    def _checklist_items(self, connection, checklist_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM inspection_items WHERE checklist_id=? ORDER BY position", (checklist_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def _require_item(self, connection, checklist_id: str, item_key: str):
        row = connection.execute(
            "SELECT * FROM inspection_items WHERE checklist_id=? AND item_key=?",
            (checklist_id, item_key),
        ).fetchone()
        if row is None:
            raise NotFoundError("应巡项目不在当日清单中")
        return row

    def generate_checklist(self, *, request_id: str, actor_id: str, site_id: str,
                           local_date: str | None = None) -> dict[str, Any]:
        """生成（或返回）某场所某当地日期的应巡清单。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator", "reviewer")
            site = self._site(connection, site_id)
            self._check_scope(actor, site)
            zone = self._zone(site)
            day = self._parse_local_date(local_date) if local_date else self._now().astimezone(zone).date()
            payload = {"actor_id": actor_id, "site_id": site_id, "local_date": day.isoformat()}

            def create() -> tuple[str, str, dict[str, Any]]:
                checklist = self._ensure_checklist(connection, site=site, local_date=day, actor_id=actor_id)
                items = self._checklist_items(connection, checklist["checklist_id"])
                response = {
                    "checklist_id": checklist["checklist_id"],
                    "site_id": site_id,
                    "local_date": day.isoformat(),
                    "version": checklist["version"],
                    "item_count": len(items),
                    "items": [{"item_key": item["item_key"], "item_type": item["item_type"],
                               "title": item["title"], "requirement": item["requirement"]}
                              for item in items],
                }
                return "inspection_checklist", checklist["checklist_id"], response

            return self._execute(connection, request_id=request_id,
                                 action="generate_inspection_checklist", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 凭据提交
    # ------------------------------------------------------------------

    def submit_evidence(self, *, request_id: str, actor_id: str, site_id: str, item_key: str,
                        evidence_hash: str, captured_at: str, storage_ref: str) -> dict[str, Any]:
        """接收一条结构化凭据；相同重试返回原回执，不同内容保留为新版本。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_scope(actor, site)
            zone = self._zone(site)
            item_key = self.domain._identifier(item_key, "item_key")
            evidence_hash = str(evidence_hash).strip().lower()
            if not HEX_DIGEST.fullmatch(evidence_hash):
                raise ValidationError("evidence_hash 必须是 64 位小写十六进制摘要")
            storage_ref = self.domain._text(storage_ref, "storage_ref", 500)
            captured = self._parse_moment(captured_at, "captured_at")
            now = self._now()
            if captured - now > CLOCK_SKEW_TOLERANCE:
                raise ValidationError("captured_at 超出允许的设备时钟偏差")
            captured_text = self._moment_text(captured)
            day = captured.astimezone(zone).date()
            payload = {"actor_id": actor_id, "site_id": site_id, "item_key": item_key,
                       "evidence_hash": evidence_hash, "captured_at": captured_text,
                       "storage_ref": storage_ref}

            def create() -> tuple[str, str, dict[str, Any]]:
                checklist = self._ensure_checklist(connection, site=site, local_date=day, actor_id=actor_id)
                self._require_item(connection, checklist["checklist_id"], item_key)
                rows = connection.execute(
                    "SELECT * FROM inspection_evidence WHERE site_id=? AND local_date=? AND item_key=? "
                    "ORDER BY version",
                    (site_id, day.isoformat(), item_key),
                ).fetchall()
                for row in rows:
                    if (row["evidence_hash"] == evidence_hash and row["captured_at"] == captured_text
                            and row["storage_ref"] == storage_ref):
                        return "inspection_evidence", row["evidence_id"], {
                            "evidence_id": row["evidence_id"],
                            "site_id": site_id,
                            "local_date": day.isoformat(),
                            "item_key": item_key,
                            "version": row["version"],
                            "checklist_version": row["checklist_version"],
                            "late": bool(row["late"]),
                            "deduplicated": True,
                            "conflict": len(rows) > 1,
                        }
                version = len(rows) + 1
                deadline = self._deadline(zone, day)
                late = now > deadline
                evidence_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO inspection_evidence(evidence_id,site_id,local_date,item_key,"
                    "checklist_id,checklist_version,version,evidence_hash,captured_at,storage_ref,"
                    "clock_skew_seconds,late,submitted_by,received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (evidence_id, site_id, day.isoformat(), item_key,
                     checklist["checklist_id"], checklist["version"], version, evidence_hash,
                     captured_text, storage_ref, (captured - now).total_seconds(),
                     1 if late else 0, actor_id, self._now_text()),
                )
                append_event(connection, actor_id=actor_id, action="inspection.evidence_submitted",
                             resource_type="inspection_evidence", resource_id=evidence_id,
                             detail={"site_id": site_id, "local_date": day.isoformat(),
                                     "item_key": item_key, "version": version,
                                     "checklist_version": checklist["version"],
                                     "evidence_hash": evidence_hash, "captured_at": captured_text,
                                     "storage_ref": storage_ref, "late": late},
                             occurred_at=self._now_text())
                return "inspection_evidence", evidence_id, {
                    "evidence_id": evidence_id,
                    "site_id": site_id,
                    "local_date": day.isoformat(),
                    "item_key": item_key,
                    "version": version,
                    "checklist_version": checklist["version"],
                    "late": late,
                    "deduplicated": False,
                    "conflict": version > 1,
                }

            return self._execute(connection, request_id=request_id,
                                 action="submit_inspection_evidence", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 纠正说明
    # ------------------------------------------------------------------

    def submit_correction(self, *, request_id: str, actor_id: str, site_id: str,
                          local_date: str, item_key: str, content: str) -> dict[str, Any]:
        """在当日截止前登记一条纠正说明。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_scope(actor, site)
            zone = self._zone(site)
            day = self._parse_local_date(local_date)
            item_key = self.domain._identifier(item_key, "item_key")
            content = self.domain._text(content, "content", 1000)
            if self._now() > self._deadline(zone, day):
                raise ConflictError("已超过当日纠正截止时间")
            payload = {"actor_id": actor_id, "site_id": site_id, "local_date": day.isoformat(),
                       "item_key": item_key, "content": content}

            def create() -> tuple[str, str, dict[str, Any]]:
                checklist = self._ensure_checklist(connection, site=site, local_date=day, actor_id=actor_id)
                self._require_item(connection, checklist["checklist_id"], item_key)
                correction_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO inspection_corrections(correction_id,site_id,local_date,item_key,"
                    "checklist_id,checklist_version,content,submitted_by,submitted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (correction_id, site_id, day.isoformat(), item_key,
                     checklist["checklist_id"], checklist["version"], content,
                     actor_id, self._now_text()),
                )
                append_event(connection, actor_id=actor_id, action="inspection.correction_submitted",
                             resource_type="inspection_correction", resource_id=correction_id,
                             detail={"site_id": site_id, "local_date": day.isoformat(),
                                     "item_key": item_key, "checklist_version": checklist["version"],
                                     "content": content},
                             occurred_at=self._now_text())
                return "inspection_correction", correction_id, {
                    "correction_id": correction_id,
                    "site_id": site_id,
                    "local_date": day.isoformat(),
                    "item_key": item_key,
                    "checklist_version": checklist["version"],
                }

            return self._execute(connection, request_id=request_id,
                                 action="submit_inspection_correction", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 监管决定
    # ------------------------------------------------------------------

    def record_decision(self, *, request_id: str, actor_id: str, site_id: str, local_date: str,
                        item_key: str, action: str, note: str = "",
                        evidence_id: str | None = None) -> dict[str, Any]:
        """监管人员发起争议、采信某个版本或要求补证。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self.domain._actor(connection, actor_id)
            self.domain._require(actor, "admin", "reviewer")
            site = self._site(connection, site_id)
            self._check_scope(actor, site)
            self._zone(site)
            day = self._parse_local_date(local_date)
            item_key = self.domain._identifier(item_key, "item_key")
            if action not in DECISION_ACTIONS:
                raise ValidationError("action 必须是 dispute、accept 或 request_supplement")
            note = str(note).strip()
            if action in ("dispute", "request_supplement") and not note:
                raise ValidationError("争议或补证要求必须填写说明")
            if len(note) > 1000:
                raise ValidationError("note 不能超过 1000 个字符")
            if action == "accept" and not evidence_id:
                raise ValidationError("采信决定必须指定 evidence_id")
            payload = {"actor_id": actor_id, "site_id": site_id, "local_date": day.isoformat(),
                       "item_key": item_key, "action": action, "note": note,
                       "evidence_id": evidence_id}

            def create() -> tuple[str, str, dict[str, Any]]:
                checklist = self._ensure_checklist(connection, site=site, local_date=day, actor_id=actor_id)
                self._require_item(connection, checklist["checklist_id"], item_key)
                if evidence_id is not None:
                    row = connection.execute(
                        "SELECT 1 FROM inspection_evidence WHERE evidence_id=? AND site_id=? "
                        "AND local_date=? AND item_key=?",
                        (evidence_id, site_id, day.isoformat(), item_key),
                    ).fetchone()
                    if row is None:
                        raise NotFoundError("采信的凭据不属于该应巡项目")
                decision_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO inspection_decisions(decision_id,site_id,local_date,item_key,action,"
                    "evidence_id,note,checklist_id,checklist_version,decided_by,decided_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, site_id, day.isoformat(), item_key, action, evidence_id, note,
                     checklist["checklist_id"], checklist["version"], actor_id, self._now_text()),
                )
                append_event(connection, actor_id=actor_id, action="inspection.decision_recorded",
                             resource_type="inspection_decision", resource_id=decision_id,
                             detail={"site_id": site_id, "local_date": day.isoformat(),
                                     "item_key": item_key, "action": action,
                                     "evidence_id": evidence_id, "note": note,
                                     "checklist_version": checklist["version"]},
                             occurred_at=self._now_text())
                return "inspection_decision", decision_id, {
                    "decision_id": decision_id,
                    "site_id": site_id,
                    "local_date": day.isoformat(),
                    "item_key": item_key,
                    "action": action,
                    "evidence_id": evidence_id,
                    "checklist_version": checklist["version"],
                }

            return self._execute(connection, request_id=request_id,
                                 action="record_inspection_decision", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 日结报告与时间线
    # ------------------------------------------------------------------

    def _item_evidence(self, connection, site_id: str, day: date, item_key: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM inspection_evidence WHERE site_id=? AND local_date=? AND item_key=? "
            "ORDER BY version",
            (site_id, day.isoformat(), item_key),
        ).fetchall()
        return [{
            "evidence_id": row["evidence_id"],
            "version": row["version"],
            "evidence_hash": row["evidence_hash"],
            "captured_at": row["captured_at"],
            "storage_ref": row["storage_ref"],
            "clock_skew_seconds": row["clock_skew_seconds"],
            "late": bool(row["late"]),
            "checklist_version": row["checklist_version"],
            "submitted_by": row["submitted_by"],
            "received_at": row["received_at"],
        } for row in rows]

    def _item_decisions(self, connection, site_id: str, day: date, item_key: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM inspection_decisions WHERE site_id=? AND local_date=? AND item_key=? "
            "ORDER BY rowid",
            (site_id, day.isoformat(), item_key),
        ).fetchall()
        return [{
            "decision_id": row["decision_id"],
            "action": row["action"],
            "evidence_id": row["evidence_id"],
            "note": row["note"],
            "checklist_version": row["checklist_version"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
        } for row in rows]

    def _item_corrections(self, connection, site_id: str, day: date, item_key: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM inspection_corrections WHERE site_id=? AND local_date=? AND item_key=? "
            "ORDER BY rowid",
            (site_id, day.isoformat(), item_key),
        ).fetchall()
        return [{
            "correction_id": row["correction_id"],
            "content": row["content"],
            "checklist_version": row["checklist_version"],
            "submitted_by": row["submitted_by"],
            "submitted_at": row["submitted_at"],
        } for row in rows]

    def daily_report(self, *, site_id: str, local_date: str | None = None,
                     actor_id: str | None = None) -> dict[str, Any]:
        """还原某场所某当地日期的完成情况、逾期原因与争议凭据。"""

        with self.database.transaction(immediate=True) as connection:
            site = self._site(connection, site_id)
            zone = self._zone(site)
            day = self._parse_local_date(local_date) if local_date else self._now().astimezone(zone).date()
            now = self._now()
            checklist = self._ensure_checklist(connection, site=site, local_date=day,
                                               actor_id=actor_id or "system")
            deadline = self._deadline(zone, day)
            deadline_text = self._moment_text(deadline)
            items = []
            for item in self._checklist_items(connection, checklist["checklist_id"]):
                versions = self._item_evidence(connection, site_id, day, item["item_key"])
                decisions = self._item_decisions(connection, site_id, day, item["item_key"])
                corrections = self._item_corrections(connection, site_id, day, item["item_key"])
                latest = decisions[-1] if decisions else None
                resolution_state = RESOLUTION_STATES[latest["action"]] if latest else "unreviewed"
                first = versions[0] if versions else None
                first_received = None
                late_by_seconds = None
                if first is not None:
                    first_received = first["received_at"]
                    late_by_seconds = max(0.0, (self._parse_moment(first_received, "received_at")
                                                - deadline).total_seconds())
                items.append({
                    "item_key": item["item_key"],
                    "item_type": item["item_type"],
                    "title": item["title"],
                    "requirement": item["requirement"],
                    "completed": bool(versions),
                    "version_count": len(versions),
                    "has_conflict": len(versions) > 1,
                    "first_received_at": first_received,
                    "late": bool(first["late"]) if first else False,
                    "late_by_seconds": late_by_seconds,
                    "resolution_state": resolution_state,
                    "accepted_evidence_id": latest["evidence_id"]
                    if latest and latest["action"] == "accept" else None,
                    "open_dispute": resolution_state == "disputed",
                    "correction_count": len(corrections),
                    "evidence": versions,
                    "decisions": decisions,
                    "corrections": corrections,
                })
            missing = [item["item_key"] for item in items if not item["completed"]]
            late_items = [item["item_key"] for item in items if item["late"]]
            conflict_items = [item["item_key"] for item in items if item["has_conflict"]]
            disputed_items = [item["item_key"] for item in items if item["open_dispute"]]
            overdue = now > deadline
            if not items:
                status = "no_items"
            elif missing:
                status = "overdue" if overdue else "pending"
            else:
                status = "complete"
            overdue_reasons = []
            if overdue:
                for key in missing:
                    overdue_reasons.append({"item_key": key, "reason": "missing_evidence",
                                            "deadline": deadline_text})
            for item in items:
                if item["late"]:
                    overdue_reasons.append({
                        "item_key": item["item_key"],
                        "reason": "first_evidence_after_deadline",
                        "deadline": deadline_text,
                        "first_received_at": item["first_received_at"],
                        "late_by_seconds": item["late_by_seconds"],
                    })
            return {
                "site_id": site_id,
                "local_date": day.isoformat(),
                "timezone_name": site["timezone_name"],
                "checklist_id": checklist["checklist_id"],
                "checklist_version": checklist["version"],
                "checklist_generated_at": checklist["generated_at"],
                "deadline": deadline_text,
                "report_generated_at": self._now_text(),
                "status": status,
                "summary": {
                    "total_items": len(items),
                    "completed_items": len(items) - len(missing),
                    "missing_items": missing,
                    "late_items": late_items,
                    "conflict_items": conflict_items,
                    "disputed_items": disputed_items,
                    "overdue_reasons": overdue_reasons,
                },
                "items": items,
            }

    def daily_timeline(self, *, site_id: str, local_date: str) -> dict[str, Any]:
        """从哈希链审计事件还原某日不可改写的操作时间线。"""

        day = self._parse_local_date(local_date)
        events = []
        for event in self.domain.audit_events():
            if not event["action"].startswith("inspection."):
                continue
            detail = event["detail"]
            if detail.get("site_id") != site_id or detail.get("local_date") != day.isoformat():
                continue
            events.append({
                "sequence": event["sequence"],
                "event_id": event["event_id"],
                "action": event["action"],
                "actor_id": event["actor_id"],
                "occurred_at": event["occurred_at"],
                "previous_hash": event["previous_hash"],
                "event_hash": event["event_hash"],
                "detail": detail,
            })
        valid, count = verify_chain(self.database.connection)
        return {"site_id": site_id, "local_date": day.isoformat(), "events": events,
                "audit_valid": valid, "audit_events": count}
