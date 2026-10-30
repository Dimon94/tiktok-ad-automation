"""财务读取私有断点：额度让出不重读首页，完整快照才交给原发布器。"""

import hashlib
import json
from datetime import datetime

from sqlalchemy import text
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting.store_models import ReportSyncPage

BASIC_KINDS = frozenset({"ad_day", "account_day", "campaign_hour"})
_METHODS = BASIC_KINDS | {"campaign_day"}


class CheckpointReader:
    def __init__(self, session, job, reader, *, scope, row_limit):
        self.session, self.job, self.reader = session, job, reader
        self.scope, self.row_limit = scope, row_limit
        self.progress = job.progress.get("basic_paging")
        if self.progress is None:
            self.observed_at = (
                session.connection()
                .execute(text("SELECT clock_timestamp()"))
                .scalar_one()
            )
            self.progress = {
                "scope": scope,
                "observed_at": self.observed_at.isoformat(),
                "pages": {},
            }
            job.progress = job.progress | {
                "snapshot_observed_at": self.observed_at.isoformat()
            }
        else:
            try:
                self.observed_at = datetime.fromisoformat(self.progress["observed_at"])
                if (
                    self.progress["scope"] != scope
                    or self.observed_at.utcoffset() is None
                ):
                    raise ValueError("scope or clock")
            except (ValueError, KeyError, TypeError) as error:
                raise DomainError(
                    "report_query_invalid", "报表断点与冻结请求不一致"
                ) from error

    def __getattr__(self, name):
        method = getattr(self.reader, name)
        if name not in _METHODS:
            return method

        def read(**arguments):
            page = arguments["page"]
            query = {k: v for k, v in arguments.items() if k != "page"}
            query_key = hashlib.sha256(
                json.dumps(
                    [self.scope, name, query], sort_keys=True, default=str
                ).encode()
            ).hexdigest()
            key = f"{query_key}:{page}"
            truncated = self.progress.get("truncated", {}).get(key)
            if truncated is not None:
                return RawReportPage((), page, truncated[0], truncated[1])
            saved = self.progress["pages"].get(key)
            if saved is not None:
                stored = self.session.get(ReportSyncPage, (self.job.id, saved))
                if (
                    stored is None
                    or stored.tenant_id != self.job.tenant_id
                    or stored.scope_key != query_key
                ):
                    raise DomainError(
                        "report_query_invalid", "报表断点证据缺失或超出范围"
                    )
                return RawReportPage(
                    tuple(stored.rows), page, stored.total_page, stored.total_number
                )
            result = method(**arguments)
            # 截断仍由原拆分器处理，不能把不完整的上限回执保存成可发布快照。
            if result.total_number >= self.row_limit:
                self.progress = self.progress | {
                    "truncated": self.progress.get("truncated", {})
                    | {key: [result.total_page, result.total_number]}
                }
                self.job.progress = self.job.progress | {"basic_paging": self.progress}
                return result
            # 单页完整主报表当场发布，无需为每轮热刷增加暂存写入。系列拆分仍要续传。
            if (
                not self.progress["pages"]
                and name == self.job.kind
                and page == 1
                and result.next_page is None
            ):
                return result
            previous = self.session.exec(
                select(ReportSyncPage).where(
                    ReportSyncPage.job_id == self.job.id,
                    ReportSyncPage.scope_key == query_key,
                )
            ).all()
            if len(previous) != page - 1 or result.page != page:
                raise DomainError("report_response_invalid", "报表断点分页不连续")
            row_keys = [
                json.dumps(row.get("dimensions"), sort_keys=True, separators=(",", ":"))
                for row in result.rows
            ]
            existing_keys = {key for p in previous for key in p.row_keys}
            if len(set(row_keys)) != len(row_keys):
                raise DomainError("report_row_invalid", "报表单页包含重复行")
            if any(
                (p.total_page, p.total_number)
                != (result.total_page, result.total_number)
                for p in previous
            ):
                raise DomainError(
                    "report_snapshot_changed", "报表分页总数变化，需要重建完整快照"
                )
            if existing_keys.intersection(row_keys):
                # 行数不变也可能因源数据排序变化跨页重叠，旧页不能拼成完整快照。
                raise DomainError(
                    "report_snapshot_changed", "报表跨页内容变化，需要重建完整快照"
                )
            if (
                result.next_page is None
                and sum(len(p.rows) for p in previous) + len(result.rows)
                != result.total_number
            ):
                raise DomainError("report_response_invalid", "报表完整分页与行数不一致")
            index = len(self.progress["pages"]) + 1
            self.session.add(
                ReportSyncPage(
                    job_id=self.job.id,
                    tenant_id=self.job.tenant_id,
                    page=index,
                    scope_key=query_key,
                    rows=list(result.rows),
                    row_keys=row_keys,
                    total_page=result.total_page,
                    total_number=result.total_number,
                )
            )
            self.progress = self.progress | {
                "pages": self.progress["pages"] | {key: index}
            }
            self.job.progress = self.job.progress | {"basic_paging": self.progress}
            self.session.flush()
            return result

        return read
