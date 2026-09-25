"""命令行还原某场所某日的自巡结论。"""

from __future__ import annotations

import argparse
import json

from .errors import DomainError
from .evidence import EvidenceService
from .storage import Database


def main() -> int:
    """打印某日是否完成、为何逾期、哪些凭据仍有争议。"""

    parser = argparse.ArgumentParser(description="还原某日自巡结论")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--site", required=True, help="场所编号")
    parser.add_argument("--date", required=True, help="企业所在地日期，YYYY-MM-DD")
    args = parser.parse_args()
    database = Database(args.database)
    try:
        service = EvidenceService(database)
        report = service.day_report(site_id=args.site, local_date=args.date)
    except DomainError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        database.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
