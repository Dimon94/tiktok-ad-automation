"""纯字段编解码；不读时钟，不填授权/素材，不修改已冻结请求。"""

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, ValidationError

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.builds import (
    CREATE_HIGHEST_VALUE_EVENT,
    AdCreate,
    AdGroupCreate,
    BuildReadQuery,
    CampaignCreate,
    CarouselCreative,
    CreateIntent,
    CtaAsset,
    CtaCreate,
    FrozenModel,
    Id,
    IdentityFields,
    NativeAdCreate,
    NativeAdGroupCreate,
    NativeAdGroupObservedFacts,
    VideoCreative,
)
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    RemoteCallError,
    exact_json_number,
)
from app.integrations.tiktok.contracts.context import ChannelKind
from app.modules.builds.targeting_schemas import AgeGroup, Gender, Language


class _CampaignBody(CampaignCreate):
    name: Id = Field(alias="campaign_name")


TARGETING_FIELDS = (
    "location_ids",
    "languages",
    "age_groups",
    "gender",
    "spc_audience_age",
)


class _Targeting(FrozenModel):
    location_ids: tuple[Id, ...] = Field(min_length=1)
    languages: tuple[Language, ...] | None = Field(default=None, min_length=1)
    age_groups: tuple[AgeGroup, ...] | None = Field(default=None, min_length=1)
    gender: Gender | None = None
    spc_audience_age: Literal["OVER_EIGHTEEN", "OVER_TWENTY_FIVE"] | None = None


class _AdGroupBody(AdGroupCreate):
    name: Id = Field(alias="adgroup_name")


class _NativeAdGroupBody(NativeAdGroupCreate):
    name: Id = Field(alias="adgroup_name")


class _NativeObservedGroupBody(NativeAdGroupObservedFacts):
    name: Id = Field(alias="adgroup_name")


class _Video(FrozenModel):
    video_id: Id
    file_name: str | None = Field(default=None, min_length=1, max_length=100)


class _Image(FrozenModel):
    web_uri: Id


class _VideoInfo(IdentityFields):
    ad_format: Literal["SINGLE_VIDEO"]
    video_info: _Video
    image_info: tuple[_Image, ...] | None = Field(
        default=None, min_length=1, max_length=1
    )


class _Music(FrozenModel):
    music_id: Id


class _CarouselInfo(IdentityFields):
    ad_format: Literal["CAROUSEL_ADS"]
    image_info: tuple[_Image, ...] = Field(min_length=1, max_length=35)
    music_info: _Music


class _Creative(FrozenModel):
    creative_info: Annotated[
        _VideoInfo | _CarouselInfo, Field(discriminator="ad_format")
    ]


class _Text(FrozenModel):
    ad_text: Id = Field(max_length=100)


class _Url(FrozenModel):
    landing_page_url: Id


class _AdConfiguration(FrozenModel):
    call_to_action_id: Id
    dark_post_status: Literal["ON", "OFF"] | None = None
    product_info_enabled: Literal["NON_CATALOG"] | None = None


class _AdBody(FrozenModel):
    advertiser_id: Id
    adgroup_id: Id
    ad_name: Id
    operation_status: Literal["ENABLE", "DISABLE"]
    creative_list: tuple[_Creative, ...] = Field(min_length=1, max_length=50)
    ad_text_list: tuple[_Text, ...] = Field(min_length=1, max_length=5)
    landing_page_url_list: tuple[_Url, ...] = Field(min_length=1, max_length=1)
    ad_configuration: _AdConfiguration


class _NativeAdBody(_AdBody):
    landing_page_url_list: None = None
    ad_text_list: tuple[_Text, ...] = Field(min_length=1, max_length=1)


class _CtaBody(FrozenModel):
    advertiser_id: Id
    creative_portfolio_type: Literal["CTA"]
    portfolio_content: tuple[CtaAsset, ...] = Field(min_length=1, max_length=50)


def _invalid(field: str, value: object) -> ValidationError:
    return ValidationError.from_exception_data(
        "CreateIntent",
        [
            {
                "type": "value_error",
                "loc": (field,),
                "input": value,
                "ctx": {"error": ValueError("invalid frozen request field")},
            }
        ],
    )


def decode_intent(kind: str, body: dict[str, object]) -> CreateIntent:
    """仅解码本地历史/待冻结输入；旧 JSON 浮点兼容不构成远端精度证据。"""
    if not isinstance(body, dict) or "kind" in body:
        raise _invalid("body", body)
    values = dict(body)
    # 策略枚举只存在于本地冻结输入；远端 wire 不允许把它们当作平台字段。
    if any(field in values for field in ("budget_strategy", "bid_strategy")):
        raise _invalid("strategy_fields", values)
    # 历史 Minis 模板的 catalog=false 不作为可观察事实；平台不会回传该字段。
    # 只允许移除明确 false，true/数字或未知值仍由严格 DTO 拒绝。
    if kind == "CAMPAIGN" and values.get("catalog_enabled") is False:
        values.pop("catalog_enabled")
    # 旧 IAA 场景核实的是 Day 0 资格；明确发送 ZERO_DAY，避免平台默认 Day 7。
    if (
        kind == "ADGROUP"
        and values.get("optimization_event") == "IMPRESSION_LEVEL_AD_REVENUE"
    ):
        values.setdefault("vbo_window", "ZERO_DAY")
    # 兼容旧冻结 JSON 数值表示，只在输入边界十进制化，不改原记录。
    for field in ("budget", "roas_bid"):
        if type(values.get(field)) is float:
            values[field] = str(values[field])
    return decode_observed_intent(kind, values)


def decode_observed_intent(
    kind: str, body: dict[str, object], *, native_observation: bool = False
) -> CreateIntent:
    """远端只接受精确金额类型；SDK 浮点可能已舍入，不能转回文本冒充精确事实。"""
    if not isinstance(body, dict) or "kind" in body:
        raise _invalid("body", body)
    values = dict(body)
    if any(field in values for field in ("budget_strategy", "bid_strategy")):
        raise _invalid("strategy_fields", values)
    if kind == "CAMPAIGN":
        parsed = _CampaignBody.model_validate(values)
        return CampaignCreate.model_validate(parsed.model_dump())
    if kind == "ADGROUP":
        if any(field in values for field in TARGETING_FIELDS):
            raise _invalid("targeting_spec", values)
        targeting = _Targeting.model_validate(values.pop("targeting_spec", None))
        values.update(targeting.model_dump(exclude_none=True))
        group_type = (
            _NativeAdGroupBody
            if values.get("promotion_type") == "NATIVE_SERIES"
            else _AdGroupBody
        )
        intent_type = (
            NativeAdGroupCreate if group_type is _NativeAdGroupBody else AdGroupCreate
        )
        if native_observation and group_type is _NativeAdGroupBody:
            parsed_group = _NativeObservedGroupBody.model_validate(values)
            return NativeAdGroupObservedFacts.model_validate(parsed_group.model_dump())
        parsed_group = group_type.model_validate(values)
        return intent_type.model_validate(parsed_group.model_dump())
    if kind == "AD":
        native = "landing_page_url_list" not in values
        ad = (_NativeAdBody if native else _AdBody).model_validate(values)
        info = ad.creative_list[0].creative_info
        identity = (
            info.identity_type,
            info.identity_id,
            info.identity_authorized_bc_id,
        )
        if any(
            (
                item.creative_info.identity_type,
                item.creative_info.identity_id,
                item.creative_info.identity_authorized_bc_id,
            )
            != identity
            for item in ad.creative_list
        ):
            raise _invalid("creative_list.identity", values["creative_list"])
        return (NativeAdCreate if native else AdCreate)(
            advertiser_id=ad.advertiser_id,
            adgroup_id=ad.adgroup_id,
            name=ad.ad_name,
            operation_status=ad.operation_status,
            identity_type=info.identity_type,
            identity_id=info.identity_id,
            identity_authorized_bc_id=info.identity_authorized_bc_id,
            text=ad.ad_text_list[0].ad_text,
            text_variants=tuple(item.ad_text for item in ad.ad_text_list)
            if len(ad.ad_text_list) > 1
            else None,
            dark_post_status=ad.ad_configuration.dark_post_status,
            product_info_enabled=ad.ad_configuration.product_info_enabled,
            landing_page_url=None
            if native
            else ad.landing_page_url_list[0].landing_page_url,
            portfolio_id=ad.ad_configuration.call_to_action_id,
            assets=tuple(
                (
                    VideoCreative(
                        kind="VIDEO",
                        video_id=item.creative_info.video_info.video_id,
                        file_name=item.creative_info.video_info.file_name,
                        cover_image_id=item.creative_info.image_info[0].web_uri
                        if item.creative_info.image_info
                        else None,
                    )
                    if isinstance(item.creative_info, _VideoInfo)
                    else CarouselCreative(
                        kind="CAROUSEL",
                        image_ids=tuple(
                            image.web_uri for image in item.creative_info.image_info
                        ),
                        music_id=item.creative_info.music_info.music_id,
                    )
                )
                for item in ad.creative_list
            ),
        )
    if kind == "CTA":
        cta = _CtaBody.model_validate(values)
        return CtaCreate(advertiser_id=cta.advertiser_id, assets=cta.portfolio_content)
    raise _invalid("kind", kind)


def _asset_info(asset: VideoCreative | CarouselCreative) -> dict[str, Any]:
    if isinstance(asset, CarouselCreative):
        return {
            "ad_format": "CAROUSEL_ADS",
            "image_info": [{"web_uri": image} for image in asset.image_ids],
            "music_info": {"music_id": asset.music_id},
        }
    return {
        "ad_format": "SINGLE_VIDEO",
        "video_info": {
            "video_id": asset.video_id,
            **({"file_name": asset.file_name} if asset.file_name else {}),
        },
        **(
            {"image_info": [{"web_uri": asset.cover_image_id}]}
            if asset.cover_image_id
            else {}
        ),
    }


def encode_intent(intent: CreateIntent) -> dict[str, object]:
    """持久请求 JSON 使用精确十进制字符串；发送适配器另行处理 wire 数字。"""
    body = intent.model_dump(mode="json", exclude={"kind"}, exclude_none=True)
    # budget_strategy/bid_strategy 是本地冻结合同，用于校验和回读映射，
    # TikTok wire 只接受它们展开后的字段，不能把内部枚举发送到远端。
    body.pop("budget_strategy", None)
    body.pop("bid_strategy", None)
    if isinstance(intent, CampaignCreate):
        body["campaign_name"] = body.pop("name")
    elif isinstance(intent, AdGroupCreate):
        body["adgroup_name"] = body.pop("name")
        body["targeting_spec"] = {
            field: body.pop(field) for field in TARGETING_FIELDS if field in body
        }
    elif isinstance(intent, AdCreate):
        return {
            "advertiser_id": intent.advertiser_id,
            "adgroup_id": intent.adgroup_id,
            "ad_name": intent.name,
            "operation_status": intent.operation_status,
            "creative_list": [
                {
                    "creative_info": {
                        "identity_type": intent.identity_type,
                        "identity_id": intent.identity_id,
                        **(
                            {
                                "identity_authorized_bc_id": intent.identity_authorized_bc_id
                            }
                            if intent.identity_authorized_bc_id is not None
                            else {}
                        ),
                        **_asset_info(asset),
                    }
                }
                for asset in intent.assets
            ],
            "ad_text_list": [
                {"ad_text": text} for text in intent.text_variants or (intent.text,)
            ],
            **(
                {
                    "landing_page_url_list": [
                        {"landing_page_url": intent.landing_page_url}
                    ]
                }
                if intent.landing_page_url is not None
                else {}
            ),
            "ad_configuration": {
                "call_to_action_id": intent.portfolio_id,
                **{
                    key: getattr(intent, key)
                    for key in ("dark_post_status", "product_info_enabled")
                    if getattr(intent, key) is not None
                },
            },
        }
    elif isinstance(intent, CtaCreate):
        return {
            "advertiser_id": intent.advertiser_id,
            "creative_portfolio_type": "CTA",
            "portfolio_content": body["assets"],
        }
    return body


READ_OPERATIONS = {
    "CAMPAIGN": "build.get_campaigns",
    "ADGROUP": "build.get_adgroups",
    "AD": "build.get_ads",
    "CTA": "build.get_cta_portfolio",
    "ADGROUP_STATUS": "build.get_regular_adgroups",
}


def read_arguments(query: BuildReadQuery) -> tuple[str, dict[str, object]]:
    """精确 ID 查询仍限定父级；CTA 无已知 ID 时不假造可完整扫描的工具。"""
    kind = query.intent.kind
    body = encode_intent(query.intent)
    arguments: dict[str, object] = {"advertiser_id": query.intent.advertiser_id}
    if kind == "CTA":
        if query.remote_id is None or query.page != 1:
            raise RemoteCallError(
                "cta_unknown_id", effect="NOT_SENT", evidence=CallEvidence()
            )
        arguments["creative_portfolio_id"] = query.remote_id
    else:
        identifiers = {
            "CAMPAIGN": "campaign_ids",
            "ADGROUP": "adgroup_ids",
            "AD": "smart_plus_ad_ids",
        }
        names = {"CAMPAIGN": "campaign_name", "ADGROUP": "adgroup_name"}
        filtering: dict[str, object] = {}
        if query.remote_id is not None:
            filtering[identifiers[kind]] = [query.remote_id]
        elif kind in names:
            filtering[names[kind]] = body[names[kind]]
        if isinstance(query.intent, (AdGroupCreate, NativeAdGroupCreate)):
            filtering["campaign_ids"] = [query.intent.campaign_id]
        elif isinstance(query.intent, (AdCreate, NativeAdCreate)):
            filtering["adgroup_ids"] = [query.intent.adgroup_id]
        fields = list(body)
        fields.extend([identifiers[kind][:-1], "secondary_status"])
        arguments.update(filtering=filtering, page=query.page, page_size=100)
        # 原生 Smart+ 广告组的字段筛选会漏掉 targeting_spec 和状态；读取完整对象。
        # 仍严格校验父级、分页及所有冻结字段，不从请求补造缺失事实。
        if kind != "ADGROUP":
            arguments["fields"] = fields
    return READ_OPERATIONS[kind], arguments


class _StatusQuery(FrozenModel):
    advertiser_id: Id
    adgroup_id: Id


def status_arguments(*, advertiser_id: str, adgroup_id: str) -> dict[str, object]:
    _StatusQuery(advertiser_id=advertiser_id, adgroup_id=adgroup_id)
    return {
        "advertiser_id": advertiser_id,
        "filtering": {"adgroup_ids": [adgroup_id]},
        "fields": ["advertiser_id", "adgroup_id", "operation_status"],
        "page": 1,
        "page_size": 100,
    }


CREATE_OPERATIONS = {
    "CAMPAIGN": "build.create_campaign",
    "ADGROUP": "build.create_adgroup",
    "AD": "build.create_ad",
    "CTA": "build.create_cta_portfolio",
}


def remote_request_id(attempt_id: UUID) -> str:
    """官方创建接口只接受 int64 字符串；本地 UUID 仍用于完整的 attempt 归属。"""
    return str((attempt_id.int & ((1 << 63) - 1)) or 1)


def create_arguments(
    *, attempt_id: UUID, intent: CreateIntent, channel: ChannelKind
) -> tuple[str, dict[str, object]]:
    """attempt 仅做本地关联；金额必须能由两条官方 JSON 路径无损表达。"""
    if channel not in {"OFFICIAL_API", "OFFICIAL_MCP"} or not isinstance(
        attempt_id, UUID
    ):
        raise RemoteCallError(
            "invalid_build_request", effect="NOT_SENT", evidence=CallEvidence()
        )
    # 历史MANUAL请求仍可解码和回读，但任何发送通道都不能再次提交不支持的条件。
    if isinstance(intent, AdGroupCreate) and (
        intent.targeting_optimization_mode == "MANUAL"
        or intent.age_groups is not None
        or intent.gender not in (None, "GENDER_UNLIMITED")
    ):
        raise RemoteCallError(
            "targeting_minis_unsupported", effect="NOT_SENT", evidence=CallEvidence()
        )
    body = encode_intent(intent)
    for field in ("budget", "roas_bid"):
        if field not in body:
            continue
        number = exact_json_number(Decimal(str(body[field])))
        if number is None:
            raise RemoteCallError(
                "decimal_serialization_loss", effect="NOT_SENT", evidence=CallEvidence()
            )
        body[field] = number
    if intent.kind in {"CAMPAIGN", "ADGROUP"}:
        body["request_id"] = remote_request_id(attempt_id)
    return CREATE_OPERATIONS[intent.kind], body


PROTECTED = frozenset(
    {
        "advertiser_id",
        "campaign_id",
        "adgroup_id",
        "campaign_name",
        "adgroup_name",
        "ad_name",
        "budget",
        "budget_optimize_on",
        "roas_bid",
        "operation_status",
        "budget_strategy",
        "bid_strategy",
    }
)

APPLICATION_COPY_MAX_CHARACTERS = 100


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def ad_assets(
    mappings: Sequence[VideoCreative | CarouselCreative],
    *,
    text: str,
    url: str,
    identity: dict[str, str],
) -> dict[str, Any]:
    """One SP text, the whole group, and verified target-account video/cover IDs."""
    if not mappings:
        raise DomainError("empty_material_group", "素材组不能为空")
    if len(mappings) > 50 or not _nonempty(text) or not _nonempty(url):
        raise DomainError("invalid_build_request", "创意信息无效")
    try:
        IdentityFields.model_validate(identity)
    except ValidationError:
        raise DomainError("invalid_build_request", "创意身份归属无效") from None
    if len(text) > APPLICATION_COPY_MAX_CHARACTERS:
        raise DomainError("copy_too_long", "应用文案策略最多允许 100 个字符")
    creatives = []
    for item in mappings:
        if not isinstance(item, (VideoCreative, CarouselCreative)):
            raise DomainError("target_asset_incomplete", "目标账户素材尚未核实")
        creatives.append({"creative_info": {**identity, **_asset_info(item)}})
    return {
        "creative_list": creatives,
        "ad_text_list": [{"ad_text": text}],
        "landing_page_url_list": [{"landing_page_url": url}],
    }


def cta_portfolio(
    *, advertiser_id: str, assets: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Keep the recommended CTA text bound to its actual asset IDs."""
    if not _nonempty(advertiser_id) or not 1 <= len(assets) <= 50:
        raise DomainError("cta_unavailable", "缺少当前账户的 CTA 推荐证据")
    content = []
    seen: set[str] = set()
    for asset in assets:
        ids = asset.get("asset_ids")
        if (
            not _nonempty(asset.get("asset_content"))
            or not isinstance(ids, (tuple, list))
            or not 1 <= len(ids) <= 50
            or not all(_nonempty(value) for value in ids)
        ):
            raise DomainError("cta_unavailable", "CTA 推荐证据不完整")
        seen.update(ids)
        content.append(
            {"asset_ids": list(ids), "asset_content": asset["asset_content"]}
        )
    if len(seen) > 50:
        raise DomainError("cta_unavailable", "CTA 推荐证据超出支持范围")
    return {
        "advertiser_id": advertiser_id,
        "creative_portfolio_type": "CTA",
        "portfolio_content": content,
    }


def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DomainError("invalid_build_request", "搭建请求格式无效")
    try:

        def json_default(item: object) -> str:
            if isinstance(item, Decimal):
                return str(item)
            raise TypeError

        # 预览金额可能仍是 Decimal；先以文本复制，create_arguments 再做
        # 官方 JSON 数字的无损转换，避免 Python float 在编译阶段提前舍入。
        result: dict[str, Any] = json.loads(
            json.dumps(value, allow_nan=False, default=json_default)
        )
    except ValueError, TypeError, RecursionError:
        raise DomainError("invalid_build_request", "搭建请求格式无效") from None
    return result


def compile_request(
    kind: str, *, fixed: dict[str, Any], resolved: dict[str, Any]
) -> dict[str, Any]:
    if kind not in {"campaign", "adgroup", "ad"}:
        raise DomainError("invalid_build_kind", "搭建层级无效")
    if not isinstance(resolved, dict) or PROTECTED.intersection(resolved):
        raise DomainError("scene_overrides_frozen_fields", "场景不能覆盖已确认字段")
    frozen = _json_copy(fixed)
    budget_strategy = frozen.pop("budget_strategy", None)
    bid_strategy = frozen.pop("bid_strategy", None)
    creation_status = frozen.pop("creation_status", "ENABLE")
    if creation_status not in {"ENABLE", "DISABLE"}:
        raise DomainError("invalid_creation_status", "创建状态必须为 ENABLE 或 DISABLE")
    if budget_strategy not in {None, "SERIES", "ADGROUP"}:
        raise DomainError("invalid_budget_strategy", "预算策略无效")
    if bid_strategy not in {None, "HIGHEST_VALUE", "TARGET_ROAS"}:
        raise DomainError("bid_strategy_invalid", "竞价策略无效")
    body = {**_json_copy(resolved), **frozen, "operation_status": creation_status}
    if kind == "campaign":
        if budget_strategy == "ADGROUP":
            if "budget" in body:
                raise DomainError("invalid_budget_strategy", "组预算不能发送系列预算")
            # TikTok requires the explicit zero campaign budget and disabled
            # campaign optimization when budget is owned by ad groups.
            body["budget"] = 0
            body["budget_optimize_on"] = False
            body["budget_mode"] = "BUDGET_MODE_INFINITE"
            body["smart_plus_adgroup_mode"] = "MULTIPLE"
        else:
            if "budget" not in body:
                raise DomainError(
                    "invalid_budget_strategy", "系列预算缺少 Campaign 日预算"
                )
            body["budget_optimize_on"] = True
            body["budget_mode"] = "BUDGET_MODE_DYNAMIC_DAILY_BUDGET"
    if kind == "adgroup":
        if budget_strategy == "ADGROUP":
            if "budget" not in body:
                raise DomainError(
                    "invalid_budget_strategy", "组预算缺少 Ad Group 日预算"
                )
            body["budget_mode"] = "BUDGET_MODE_DYNAMIC_DAILY_BUDGET"
        elif "budget" in body:
            raise DomainError(
                "adgroup_budget_not_allowed", "系列预算策略不能设置广告组预算"
            )
        if bid_strategy == "HIGHEST_VALUE":
            if "roas_bid" in body:
                raise DomainError("bid_strategy_invalid", "最高价值不能包含 ROAS 出价")
            body.update(
                optimization_goal="VALUE",
                optimization_event=CREATE_HIGHEST_VALUE_EVENT,
                deep_bid_type="VO_HIGHEST_VALUE",
            )
        elif bid_strategy == "TARGET_ROAS":
            if "roas_bid" not in body:
                raise DomainError("bid_strategy_invalid", "目标 ROAS 缺少目标值")
            body["deep_bid_type"] = "VO_MIN_ROAS"
    return body
