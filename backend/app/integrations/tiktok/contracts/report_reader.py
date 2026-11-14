# backend/app/integrations/tiktok/contracts/report_reader.py
"""报表读取合同：只走官方 SDK；返回平台原始行，解析与发布由报表模块负责。"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.core.errors import DomainError


@dataclass(frozen=True)
class RawReportPage:
    rows: tuple[dict, ...]
    page: int
    total_page: int
    total_number: int

    @property
    def next_page(self) -> int | None:
        return self.page + 1 if self.page < self.total_page else None


def page_from_data(data: dict, *, requested_page: int) -> RawReportPage:
    """校验 page_info；合法零行时平台可能返回空对象，视为完整空结果。"""
    if data == {}:
        return RawReportPage((), requested_page, 0, 0)
    rows, info = data.get("list"), data.get("page_info")
    if (
        type(rows) is not list
        or type(info) is not dict
        or any(type(r) is not dict for r in rows)
    ):
        raise DomainError("report_response_invalid", "报表回执无效")
    page, size, total_page, total = (
        info.get(k) for k in ("page", "page_size", "total_page", "total_number")
    )
    if (
        any(type(v) is not int for v in (page, size, total_page, total))
        or page < 1
        or size < 1
        or total_page < 0
        or total < 0
    ):
        raise DomainError("report_response_invalid", "报表分页信息无效")
    expected_pages = 0 if total == 0 else (total + size - 1) // size
    expected_rows = 0 if total == 0 else min(size, max(0, total - (page - 1) * size))
    if (
        page != requested_page
        or total_page != expected_pages
        and not (total == 0 and total_page in (0, 1))
        or len(rows) != expected_rows
    ):
        raise DomainError("report_response_invalid", "报表分页与声明不一致")
    return RawReportPage(tuple(rows), page, total_page, total)


class ReportReader(Protocol):
    def bc_spend(
        self, *, bc_id: str, start: date, end: date, page: int
    ) -> RawReportPage: ...
    def ad_day(
        self,
        *,
        advertiser_ids: tuple[str, ...],
        start: date,
        end: date,
        page: int,
        campaign_ids: tuple[str, ...] = (),
    ) -> RawReportPage: ...
    def creative_day(
        self,
        *,
        advertiser_ids: tuple[str, ...],
        start: date,
        end: date,
        page: int,
        campaign_ids: tuple[str, ...] = (),
    ) -> RawReportPage: ...
    def campaign_hour(
        self, *, advertiser_ids: tuple[str, ...], day: date, page: int
    ) -> RawReportPage: ...
    def campaign_day(
        self, *, advertiser_ids: tuple[str, ...], start: date, end: date, page: int
    ) -> RawReportPage: ...

    def adgroup_day(
        self, *, advertiser_ids: tuple[str, ...], start: date, end: date, page: int
    ) -> RawReportPage: ...
    def account_day(
        self, *, advertiser_ids: tuple[str, ...], start: date, end: date, page: int
    ) -> RawReportPage: ...
