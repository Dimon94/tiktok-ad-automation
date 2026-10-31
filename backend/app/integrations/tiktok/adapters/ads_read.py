"""双通道共用的目录与财务映射；无数据库访问，不猜测跨命名空间身份。"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import (
    AccountBalance,
    AdEntity,
    AdMaterialUsage,
    DirectoryPage,
    DirectoryQuery,
    EntityKind,
    EntityRef,
    MaterialUseRef,
)
from app.integrations.tiktok.contracts.carousel import decode_carousels
from app.integrations.tiktok.contracts.common import CallEvidence, McpBusinessResponse
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute

# 同一物理端点沿用既有发送键，避免 build 回读与目录读取拆分共享额度。
ADS_OPERATIONS = {
    ("campaign", False): "ads.get_campaigns",
    ("adgroup", False): "build.get_regular_adgroups",
    ("ad", False): "ads.get_ads",
    ("campaign", True): "build.get_campaigns",
    ("adgroup", True): "build.get_adgroups",
    ("ad", True): "build.get_ads",
}

FINANCE_OPERATION = "finance.get_advertiser_balances"
# 只吞掉已分类的可选读取能力失败；路由、租约和授权代数变化必须终止本次执行。
OPTIONAL_READ_FAILURES = frozenset(
    {
        "mcp_tool_unavailable",
        "mcp_contract_changed",
        "mcp_business_error",
        "mcp_response_invalid",
        "tiktok_response_error",
        "ads_response_invalid",
    }
)


def invalid() -> DomainError:
    return DomainError("ads_response_invalid", "广告目录返回结构或身份不完整")


def identifier(value: Any) -> str:
    if type(value) is not str or not value.strip():
        raise invalid()
    return value


def paged_rows(
    data: Any, *, page: int, page_size: int, key: str = "list"
) -> tuple[list[dict[str, Any]], int | None]:
    if type(data) is not dict or type(data.get(key)) is not list:
        raise invalid()
    rows, info = data[key], data.get("page_info")
    if type(info) is not dict or any(
        type(info.get(k)) is not int
        for k in ("page", "page_size", "total_page", "total_number")
    ):
        raise invalid()
    total = info["total_number"]
    total_page = max(1, (total + page_size - 1) // page_size)
    if (
        info["page"] != page
        or info["page_size"] != page_size
        or total < 0
        or info["total_page"] not in ({0, 1} if total == 0 else {total_page})
        or page > total_page
        or len(rows) != min(page_size, max(0, total - (page - 1) * page_size))
        or any(type(row) is not dict for row in rows)
    ):
        raise invalid()
    return rows, page + 1 if page < total_page else None


def compile_query(query: DirectoryQuery) -> tuple[str, dict[str, Any]]:
    if query.ad_type not in {
        "REGULAR",
        "LEGACY_SMART_PLUS",
        "SMART_PLUS",
    } or query.kind not in {"campaign", "adgroup", "ad", "creative"}:
        raise DomainError("ads_type_unavailable", "该广告类型没有已核实的读取合同")
    if query.kind == "creative" and query.ad_type != "SMART_PLUS":
        raise DomainError("ads_type_unavailable", "独立创意目录仅适用于升级 Smart+")
    smart = query.ad_type == "SMART_PLUS" and query.kind != "creative"
    kind = "ad" if query.kind == "creative" else query.kind
    maximum = 100 if smart and kind == "ad" else 1000
    if (
        query.page_size > maximum
        or max(len(query.ids), len(query.parent_ids)) > 100
        or (kind == "campaign" and query.parent_ids)
    ):
        raise DomainError("ads_query_invalid", "目录页大小或筛选范围无效")
    filtering: dict[str, Any] = {}
    if query.ids:
        filtering["smart_plus_ad_ids" if smart and kind == "ad" else f"{kind}_ids"] = (
            list(query.ids)
        )
    if query.parent_ids:
        # 创意父级是资产组；ad_ids_v2 会裁掉创意内容，不能作为其请求筛选器。
        if query.kind == "creative":
            raise DomainError(
                "ads_query_invalid",
                "创意目录须按创意 ID 读取；父级由返回的 smart_plus_ad_id 关联",
            )
        filtering["campaign_ids" if kind == "adgroup" else "adgroup_ids"] = list(
            query.parent_ids
        )
    if not smart:
        filtering["campaign_automation_type"] = {
            "REGULAR": "MANUAL",
            "LEGACY_SMART_PLUS": "SMART_PLUS",
            "SMART_PLUS": "UPGRADED_SMART_PLUS",
        }[query.ad_type]
    # 六个端点各自文档引用同一 primary_status 枚举；STATUS_ALL 包含删除对象。
    if query.include_deleted:
        filtering["primary_status"] = "STATUS_ALL"
    return ADS_OPERATIONS[kind, smart], {
        "advertiser_id": query.advertiser_id,
        "page": query.page,
        "page_size": query.page_size,
        "filtering": filtering,
    }


@dataclass
class FinanceReadState:
    """仅属于一次有期限的 gateway 会话；发送前仍校验原主体、路由及目标账户。"""

    subject_id: str | None = None
    advertiser_id: str | None = None
    verified: bool = False

    def require(self, subject_id: str | None) -> str:
        if (
            not self.verified
            or not self.subject_id
            or subject_id != self.subject_id
            or not self.advertiser_id
        ):
            raise DomainError("account_access_denied", "缺少当前 BC 的独立财务权限证据")
        return self.advertiser_id


def material_usages(
    row: dict[str, Any],
    ad_ref: EntityRef,
    *,
    smart: bool,
    creative_id: str | None = None,
) -> tuple[list[AdMaterialUsage], bool]:
    """创建回执与平台目录共用素材身份解析，不发送请求。"""
    entries = row.get("creative_list") if smart else [row]
    if type(entries) is not list:
        return [], False
    try:
        carousel = decode_carousels(
            row,
            advertiser_id=ad_ref.advertiser_id,
            ad_id=ad_ref.remote_id,
            smart=smart,
            creative_id=creative_id,
        )
    except ValueError as exc:
        raise invalid() from exc
    usages = []
    complete = carousel.complete
    for entry in entries:
        if type(entry) is not dict:
            raise invalid()
        info = entry.get("creative_info", {}) if smart else entry
        if type(info) is not dict:
            raise invalid()
        video = info.get("video_info") if smart else None
        if video is not None and type(video) is not dict:
            raise invalid()
        video_id = video.get("video_id") if video else info.get("video_id")
        assets: list[tuple[str, str]] = []
        if video_id is not None:
            assets.append((identifier(video_id), "VIDEO"))
        elif info.get("tiktok_item_id") is not None:
            # 帖子是独立平台命名空间；不能伪造素材库 VID、主素材报表类型或本地文件关联。
            assets.append((identifier(info["tiktok_item_id"]), "TIKTOK_POST"))
        else:
            # 视频封面不是独立主素材；只有无视频的图片广告登记图片使用。
            images = info.get("image_info", []) if smart else info.get("image_ids", [])
            if type(images) is not list:
                raise invalid()
            for item in images:
                image_id = (
                    item.get("image_id", item.get("web_uri"))
                    if type(item) is dict
                    else item
                )
                if image_id is None:
                    complete = False
                    continue
                assets.append((identifier(image_id), "IMAGE"))
        if not assets:
            complete = False
            continue
        ad_material_id = entry.get("ad_material_id") if smart or creative_id else None
        if ad_material_id is not None:
            identifier(ad_material_id)
        name = (
            info.get("material_name", "") if smart else entry.get("material_name", "")
        )
        if type(name) is not str:
            raise invalid()
        status = (
            entry.get("material_operation_status")
            if smart
            else entry.get("operation_status")
        )
        if status is not None:
            identifier(status)
        native_creative = entry.get("smart_plus_creative_id") if smart else creative_id
        if native_creative is not None:
            identifier(native_creative)
        for asset_id, asset_type in assets:
            usages.append(
                AdMaterialUsage(
                    MaterialUseRef(ad_ref, asset_id, ad_material_id, asset_type),
                    None,
                    status,
                    True,
                    name,
                    creative_ids=(native_creative,) if native_creative else (),
                )
            )
    return usages, complete


class AdsReadAdapter:
    def __init__(
        self,
        *,
        route: FrozenTikTokRoute,
        check_account: Callable[[str], str],
        check_accounts: Callable[[tuple[str, ...]], dict[str, str]],
        subject_id: str | None,
        finance: FinanceReadState,
    ):
        self.route = route
        self._check_account = check_account
        self._check_accounts = check_accounts
        self._subject_id = subject_id
        self._finance = finance

    def _call(self, operation: str, arguments: dict[str, Any]) -> McpBusinessResponse:
        raise NotImplementedError

    def _entity(self, row: dict[str, Any], query: DirectoryQuery) -> AdEntity:
        if row.get("advertiser_id") != query.advertiser_id:
            raise invalid()
        kind = cast(EntityKind, query.kind)
        key = (
            "smart_plus_ad_id"
            if kind == "ad" and query.ad_type == "SMART_PLUS"
            else "ad_id"
            if kind == "creative"
            else f"{kind}_id"
        )
        remote_id = identifier(row.get(key))
        if query.ids and remote_id not in query.ids:
            raise invalid()
        parents: dict[str, EntityKind] = {
            "adgroup": "campaign",
            "ad": "adgroup",
            "creative": "ad",
        }
        parent_kind = parents.get(kind)
        # 创意行的父资产组字段是 smart_plus_ad_id；ad_id_v2 属于资产组摘要合同。
        parent_id = (
            identifier(
                row.get(
                    "smart_plus_ad_id" if kind == "creative" else f"{parent_kind}_id"
                )
            )
            if parent_kind
            else None
        )
        if query.parent_ids and parent_id not in query.parent_ids:
            raise invalid()
        if kind == "creative" and parent_id == remote_id:
            raise invalid()
        name = row.get("ad_name" if kind == "creative" else f"{kind}_name", "")
        if type(name) is not str:
            raise invalid()
        for field in ("operation_status", "review_status", "secondary_status"):
            if row.get(field) is not None:
                identifier(row[field])
        return AdEntity(
            EntityRef(self.route.tenant_id, query.advertiser_id, kind, remote_id),
            EntityRef(self.route.tenant_id, query.advertiser_id, parent_kind, parent_id)
            if parent_kind and parent_id
            else None,
            query.ad_type,
            name,
            row,
            row.get("operation_status"),
            row.get("review_status"),
            row.get("secondary_status"),
            datetime.now(UTC),
        )

    def _materials(
        self,
        row: dict[str, Any],
        ad_ref: EntityRef,
        *,
        smart: bool,
        creative_id: str | None = None,
    ) -> tuple[list[AdMaterialUsage], bool]:
        return material_usages(row, ad_ref, smart=smart, creative_id=creative_id)

    def read_page(self, query: DirectoryQuery) -> DirectoryPage:
        return self._read_page(query, defer_materials=False)

    def read_primary_page(self, query: DirectoryQuery) -> DirectoryPage:
        """持久目录先交付主响应；素材由独立子运行逐页提交，避免重放主请求。"""
        return self._read_page(query, defer_materials=True)

    def _read_page(
        self, query: DirectoryQuery, *, defer_materials: bool
    ) -> DirectoryPage:
        operation, arguments = compile_query(query)
        # 目录读取前核对当前账户权限；物理发送边界仍重复核验冻结路由。
        self._check_account(query.advertiser_id)
        response = self._call(operation, arguments)
        rows, next_page = paged_rows(
            response.data, page=query.page, page_size=query.page_size
        )
        items: list[AdEntity] = []
        materials: list[AdMaterialUsage] = []
        materials_complete = True
        for row in rows:
            entity = self._entity(row, query)
            if any(item.ref == entity.ref for item in items):
                raise invalid()
            items.append(entity)
            if query.kind in {"ad", "creative"}:
                ad_ref = entity.parent_ref if query.kind == "creative" else entity.ref
                assert ad_ref is not None
                usage, complete = self._materials(
                    row,
                    ad_ref,
                    smart=query.ad_type == "SMART_PLUS" and query.kind == "ad",
                    creative_id=entity.ref.remote_id
                    if query.kind == "creative"
                    else None,
                )
                materials.extend(usage)
                materials_complete &= complete
        missing = None
        if (
            defer_materials
            and query.kind == "ad"
            and query.ad_type == "SMART_PLUS"
            and items
        ):
            materials_complete = False
            missing = "material_supplement_pending"
        elif query.kind == "ad" and query.ad_type == "SMART_PLUS" and items:
            try:
                added, usages, complete = self._supplement(query, items)
                items.extend(added)
                for usage in usages:
                    linked = [
                        existing
                        for existing in materials
                        if existing.use_ref.ad_ref == usage.use_ref.ad_ref
                        and existing.use_ref.platform_material_id
                        == usage.use_ref.platform_material_id
                        and existing.use_ref.material_type
                        == usage.use_ref.material_type
                        and set(existing.creative_ids) & set(usage.creative_ids)
                    ]
                    if len(linked) > 1:
                        raise invalid()
                    if linked and usage.use_ref.ad_material_id is None:
                        # 仅凭真实 creative_id 交集确认独立素材 ID；不得仅按 VID 猜关联。
                        usage = replace(
                            linked[0],
                            creative_ids=tuple(
                                dict.fromkeys(
                                    (*linked[0].creative_ids, *usage.creative_ids)
                                )
                            ),
                        )
                    materials.append(usage)
                materials_complete &= complete
                if not complete:
                    missing = "CREATIVE_ASSOCIATION_INCOMPLETE"
            except DomainError as error:
                # 主目录已经读取成功，素材补读没额度时保留该页并明确标记素材不完整，
                # 避免丢掉成功结果后反复消耗主请求额度；主请求及授权错误仍直接退出。
                if (
                    error.code not in OPTIONAL_READ_FAILURES
                    and error.code != "admission_deferred"
                ):
                    raise
                materials_complete = False
                missing = error.code
        if not materials_complete:
            materials = [replace(usage, complete=False) for usage in materials]
            missing = missing or "MATERIAL_SHAPE_UNAVAILABLE"
        merged: dict[MaterialUseRef, AdMaterialUsage] = {}
        for usage in materials:
            previous = merged.get(usage.use_ref)
            if previous is None:
                merged[usage.use_ref] = usage
            else:
                if previous.operation_status != usage.operation_status:
                    raise invalid()
                merged[usage.use_ref] = replace(
                    previous,
                    creative_ids=tuple(
                        dict.fromkeys((*previous.creative_ids, *usage.creative_ids))
                    ),
                )
        materials = list(merged.values())
        self._check_account(query.advertiser_id)
        return DirectoryPage(
            tuple(items),
            tuple(materials),
            next_page,
            next_page is None,
            response.evidence,
            query.page,
            materials_complete,
            missing,
        )

    def read_material_page(
        self,
        query: DirectoryQuery,
        *,
        ads: tuple[AdEntity, ...],
        materials: tuple[AdMaterialUsage, ...],
    ) -> DirectoryPage:
        """一次只读一个素材子页；准入失败原样抛出，由持久运行保留页码。"""
        if (
            query.kind != "creative"
            or query.ad_type != "SMART_PLUS"
            or query.ids
            or not ads
            or query.page_size > 1000
        ):
            raise DomainError("ads_query_invalid", "素材补读范围无效")
        owners = {ad.ref for ad in ads}
        groups = {ad.parent_ref.remote_id for ad in ads if ad.parent_ref}
        if (
            len(owners) != len(ads)
            or not groups
            or len(groups) > 100
            or any(
                ad.ref.tenant_id != self.route.tenant_id
                or ad.ref.advertiser_id != query.advertiser_id
                or ad.ref.kind != "ad"
                or ad.ad_type != "SMART_PLUS"
                or ad.parent_ref is None
                or ad.parent_ref.kind != "adgroup"
                or ad.parent_ref.tenant_id != ad.ref.tenant_id
                or ad.parent_ref.advertiser_id != ad.ref.advertiser_id
                for ad in ads
            )
            or any(usage.use_ref.ad_ref not in owners for usage in materials)
            or query.parent_ids
            and set(query.parent_ids) != {ref.remote_id for ref in owners}
        ):
            raise DomainError("directory_scope_mismatch", "素材补读超出冻结父身份")
        self._check_account(query.advertiser_id)
        items, usages, known, next_page, evidence = self._supplement_page(
            query, list(ads), page=query.page, page_size=query.page_size
        )
        # 一次索引冻结桥，避免每个子页素材反复线性扫描数万主响应身份。
        bridges: dict[tuple, dict[MaterialUseRef, AdMaterialUsage]] = {}
        for original in materials:
            use = original.use_ref
            for creative_id in original.creative_ids:
                key = (
                    use.ad_ref,
                    use.platform_material_id,
                    use.material_type,
                    creative_id,
                )
                bridges.setdefault(key, {})[use] = original
        merged: dict[MaterialUseRef, AdMaterialUsage] = {}
        for usage in usages:
            use = usage.use_ref
            linked: dict[MaterialUseRef, AdMaterialUsage] = {}
            for creative_id in usage.creative_ids:
                linked.update(
                    bridges.get(
                        (
                            use.ad_ref,
                            use.platform_material_id,
                            use.material_type,
                            creative_id,
                        ),
                        {},
                    )
                )
            if len(linked) > 1:
                raise invalid()
            if linked:
                # 精确桥的素材状态与子响应的创意广告状态是不同层级，不能相互覆盖。
                original = next(iter(linked.values()))
                if (
                    use.ad_material_id is not None
                    and original.use_ref.ad_material_id is not None
                    and use.ad_material_id != original.use_ref.ad_material_id
                ):
                    raise invalid()
                usage = replace(
                    usage,
                    use_ref=replace(
                        use,
                        ad_material_id=use.ad_material_id
                        or original.use_ref.ad_material_id,
                    ),
                    local_material_id=original.local_material_id,
                    operation_status=original.operation_status,
                    name=original.name,
                )
            usage = replace(usage, complete=usage.complete and known)
            previous = merged.get(usage.use_ref)
            if previous is not None:
                if previous.operation_status != usage.operation_status:
                    raise invalid()
                usage = replace(
                    usage,
                    creative_ids=tuple(
                        dict.fromkeys((*previous.creative_ids, *usage.creative_ids))
                    ),
                )
            merged[usage.use_ref] = usage
        self._check_account(query.advertiser_id)
        return DirectoryPage(
            tuple(items),
            tuple(merged.values()),
            next_page,
            next_page is None,
            evidence,
            query.page,
            known,
            None if known else "CREATIVE_ASSOCIATION_INCOMPLETE",
        )

    def _supplement_page(self, query, ads, *, page, page_size):
        ad_refs = {ad.ref.remote_id: ad.ref for ad in ads}
        parent_refs = {ad.ref.remote_id: ad.parent_ref for ad in ads}
        groups = sorted({ad.parent_ref.remote_id for ad in ads if ad.parent_ref})
        filtering: dict[str, Any] = {
            "adgroup_ids": groups,
            "campaign_automation_type": "UPGRADED_SMART_PLUS",
        }
        if query.include_deleted:
            filtering["primary_status"] = "STATUS_ALL"
        response = self._call(
            ADS_OPERATIONS["ad", False],
            {
                "advertiser_id": query.advertiser_id,
                "filtering": filtering,
                "page": page,
                "page_size": page_size,
            },
        )
        rows, next_page = paged_rows(response.data, page=page, page_size=page_size)
        creatives, materials = [], []
        complete = True
        seen: set[str] = set()
        for row in rows:
            if (
                row.get("advertiser_id") != query.advertiser_id
                or row.get("adgroup_id") not in groups
            ):
                raise invalid()
            if not row.get("smart_plus_ad_id") or not row.get("ad_id"):
                complete = False
                continue
            asset_id = identifier(row["smart_plus_ad_id"])
            identifier(row["ad_id"])
            if asset_id not in ad_refs:
                continue
            expected_group = parent_refs[asset_id]
            if expected_group is None or row["adgroup_id"] != expected_group.remote_id:
                raise invalid()
            entity = self._entity(
                row, replace(query, kind="creative", ids=(), parent_ids=())
            )
            if entity.ref.remote_id in seen:
                raise invalid()
            seen.add(entity.ref.remote_id)
            creatives.append(entity)
            usage, known = self._materials(
                row, ad_refs[asset_id], smart=False, creative_id=entity.ref.remote_id
            )
            materials.extend(usage)
            complete &= known
        return creatives, materials, complete, next_page, response.evidence

    def _supplement(
        self, query: DirectoryQuery, ads: list[AdEntity]
    ) -> tuple[list[AdEntity], list[AdMaterialUsage], bool]:
        creatives, materials = [], []
        complete = True
        seen: set[str] = set()
        page = 1
        while True:
            added, usages, known, next_page, _evidence = self._supplement_page(
                query, ads, page=page, page_size=1000
            )
            for entity in added:
                if entity.ref.remote_id in seen:
                    raise invalid()
                seen.add(entity.ref.remote_id)
            creatives.extend(added)
            materials.extend(usages)
            complete &= known
            if next_page is None:
                break
            page = next_page
        return creatives, materials, complete

    def read_balance(self, advertiser_id: str) -> AccountBalance:
        currency = self._check_account(advertiser_id)
        now = datetime.now(UTC)
        evidence = CallEvidence()

        def unavailable(reason: str) -> AccountBalance:
            self._check_account(advertiser_id)
            return AccountBalance(None, currency, reason, now, evidence)

        self._finance.verified = False
        if not self._subject_id:
            return unavailable("PERMISSION_UNVERIFIED")
        try:
            allowed, evidence = self._verify_finance(advertiser_id)
            if not allowed:
                return unavailable("PERMISSION_UNVERIFIED")
            page, balances = 1, []
            while True:
                response = self._call(
                    FINANCE_OPERATION,
                    {
                        "bc_id": self.route.bc_id,
                        "page": page,
                        "page_size": 1,
                        "fields": ["balance_info"],
                    },
                )
                evidence = response.evidence
                rows, next_page = paged_rows(
                    response.data, page=page, page_size=1, key="advertiser_account_list"
                )
                balances.extend(
                    (row, response.evidence)
                    for row in rows
                    if row.get("advertiser_id") == advertiser_id
                )
                if next_page is None:
                    break
                page = next_page
            self._check_account(advertiser_id)
            if len(balances) != 1:
                return unavailable(
                    "BALANCE_MISSING" if not balances else "BALANCE_AMBIGUOUS"
                )
            row, evidence = balances[0]
            return self._balance_value(
                advertiser_id, row, evidence, now, self._check_account(advertiser_id)
            )
        except DomainError as error:
            if error.code not in OPTIONAL_READ_FAILURES:
                raise
            return unavailable(error.code)
        finally:
            self._finance.verified = False

    def _verify_finance(self, advertiser_id: str) -> tuple[bool, CallEvidence]:
        matches = []
        page = 1
        evidence = CallEvidence()
        self._finance.verified = False
        if not self._subject_id:
            return False, evidence
        while True:
            response = self._call(
                "accounts.list_bc_members",
                {"bc_id": self.route.bc_id, "page": page, "page_size": 20},
            )
            evidence = response.evidence
            rows, next_page = paged_rows(response.data, page=page, page_size=20)
            matches.extend(
                row for row in rows if row.get("user_id") == self._subject_id
            )
            if next_page is None:
                break
            page = next_page
        if (
            len(matches) != 1
            or matches[0].get("relation_status") != "BOUND"
            or type(matches[0].get("ext_user_role")) is not dict
            or matches[0]["ext_user_role"].get("finance_role")
            not in {"MANAGER", "ANALYST"}
        ):
            return False, evidence
        self._finance.subject_id = self._subject_id
        self._finance.advertiser_id = advertiser_id
        self._finance.verified = True
        return True, evidence

    def _balance_value(self, advertiser_id, row, evidence, now, currency):

        def unavailable(reason):
            return AccountBalance(None, currency, reason, now, evidence)

        if row.get("currency") != currency:
            return unavailable("CURRENCY_UNVERIFIED")
        # 默认顶层数额可能属于共享 Payment Portfolio；缺少明确 scope 不当作账户余额。
        nested = row.get("balance_info")
        portfolio_id = row.get("payment_portfolio_id")
        scope: Literal["ADVERTISER", "PORTFOLIO", "UNKNOWN"]
        if type(nested) is dict:
            amount = nested.get("account_balance")
            scope, scope_id = "ADVERTISER", advertiser_id
        elif portfolio_id:
            amount = row.get("account_balance")
            scope, scope_id = (
                "PORTFOLIO",
                identifier(
                    str(portfolio_id) if type(portfolio_id) is int else portfolio_id
                ),
            )
        else:
            return unavailable("BALANCE_SCOPE_UNVERIFIED")
        if type(amount) not in {str, int}:
            return unavailable("BALANCE_MISSING")
        try:
            decimal = Decimal(amount)
        except InvalidOperation:
            return unavailable("BALANCE_MISSING")
        if not decimal.is_finite():
            return unavailable("BALANCE_MISSING")
        return AccountBalance(
            decimal,
            currency,
            "AVAILABLE",
            now,
            evidence,
            scope,
            scope_id,
        )

    def read_balance_page(
        self, advertiser_ids: tuple[str, ...], *, page: int = 1
    ) -> tuple[dict[str, AccountBalance], int | None]:
        """每次只读 BC 余额的一页，调用方持久派发下一页，避免逐账户全表扫描。"""
        if not advertiser_ids or page < 1:
            raise invalid()
        self._check_accounts(advertiser_ids)
        now = datetime.now(UTC)
        allowed, evidence = self._verify_finance(advertiser_ids[0])
        if not allowed:
            currencies = self._check_accounts(advertiser_ids)
            return {
                adv: AccountBalance(
                    None,
                    currencies[adv],
                    "PERMISSION_UNVERIFIED",
                    now,
                    evidence,
                )
                for adv in advertiser_ids
            }, None
        try:
            response = self._call(
                FINANCE_OPERATION,
                {
                    "bc_id": self.route.bc_id,
                    "page": page,
                    "page_size": 100,
                    "fields": ["balance_info"],
                },
            )
            rows, next_page = paged_rows(
                response.data, page=page, page_size=100, key="advertiser_account_list"
            )
            currencies = self._check_accounts(advertiser_ids)
            wanted = set(advertiser_ids)
            results = {}
            for row in rows:
                adv = row.get("advertiser_id")
                if adv not in wanted:
                    continue
                if adv in results:
                    raise invalid()
                results[adv] = self._balance_value(
                    adv, row, response.evidence, now, currencies[adv]
                )
            if next_page is None:
                for adv in wanted - results.keys():
                    results[adv] = AccountBalance(
                        None,
                        currencies[adv],
                        "BALANCE_MISSING",
                        now,
                        response.evidence,
                    )
            return results, next_page
        finally:
            self._finance.verified = False
