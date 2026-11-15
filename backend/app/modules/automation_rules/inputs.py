"""判断输入：展开租户账户、加载对象目录、报表事实与新鲜度。只读本地已同步数据。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.modules.accounts.access import usable_grants
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.automation_rules.catalog import RULE_METRICS
from app.modules.automation_rules.schedule import due_at, next_run_at
from app.modules.automation_rules.schemas import AccountScope, RuleDefinition
from app.modules.automation_rules.statuses import hierarchy_statuses
from app.modules.reporting.freshness import fresh_report_success
from app.modules.reporting.metric_catalog import STORED_KEYS

if TYPE_CHECKING:  # 避免运行时循环导入，同时保留作用域合同。
    from app.modules.ai_workbench.rule_binding import RuleBindingScope

_PAST = {"PAST_3_DAYS": 3, "PAST_5_DAYS": 5, "PAST_7_DAYS": 7}
_LIFETIME_START = date(2000, 1, 1)
_BUDGET_KEYS = ("budget", "daily_budget", "campaign_daily_budget")
_ROAS_KEYS = ("roas_bid", "target_roas")
_PARENT = {"adgroup": "campaign", "ad": "adgroup"}


@dataclass(frozen=True)
class AccountInfo:
    advertiser_id: str
    bc_id: str
    timezone: str
    currency: str
    name: str


@dataclass(frozen=True)
class Entity:
    key: str
    kind: str
    advertiser_id: str
    bc_id: str
    remote_id: str
    name: str
    drama: str | None
    naming_valid: bool
    status: str | None
    currency: str
    timezone: str
    parent_key: str | None
    attributes: dict[str, Decimal | str | None]
    material_uses: tuple[dict[str, Any], ...] = ()
    # 素材所属视频，与素材报表按视频跨账户合并的口径一致；对不上视频时为空。
    video: str | None = None
    # 一个视频可能对应多个历史创意；金额按本广告内视频合计，操作只用当前身份。
    report_ids: tuple[str, ...] = ()
    judgement_issue: str | None = None
    # 素材自身启用不等于可投放：其广告、广告组、系列也须启用。
    parents_enabled: bool | None = True


@dataclass
class Inputs:
    accounts: dict[str, AccountInfo]
    entities: dict[str, Entity]
    facts: dict[str, dict[str, dict[str, Decimal | None]]]
    skipped_accounts: list[dict[str, str]] = field(default_factory=list)
    # Decision evidence keeps the sync publication watermarks instead of only
    # retaining a coarse STALE_REPORT reason.
    freshness: list[dict[str, Any]] = field(default_factory=list)
    publication_versions: dict[str, int] = field(default_factory=dict)


def in_rule_scope(
    entity: Entity, definition: RuleDefinition, *, ignore_status: bool = False
) -> bool:
    """判断与缺数说明共用作用范围；反向冲突复核仍可忽略操作状态。"""
    scope = definition.apply_to
    if entity.kind != scope.level:
        return False
    if (
        not ignore_status
        and entity.kind == "material"
        and definition.action.type == "ENABLE"
        and entity.parents_enabled is False
    ):
        return False
    if scope.status == "SELECTED":
        return f"{entity.advertiser_id}:{entity.remote_id}" in scope.object_ids
    if ignore_status:
        return True
    if (
        entity.kind == "material"
        and scope.status == "ACTIVE"
        and entity.parents_enabled is False
    ):
        return False
    return entity.status == {"ACTIVE": "ENABLE", "PAUSED": "DISABLE"}[scope.status] or (
        entity.kind == "material"
        and bool(entity.judgement_issue)
        and entity.status is None
    )


def local_today(timezone: str, now: datetime) -> date:
    return now.astimezone(ZoneInfo(timezone)).date()


def local_window(range_type: str, timezone: str, now: datetime) -> tuple[date, date]:
    """按账户自身时区取日期；「最近 N 天」含今天。"""
    today = local_today(timezone, now)
    if range_type == "TODAY":
        return today, today
    if range_type == "YESTERDAY":
        return today - timedelta(days=1), today - timedelta(days=1)
    if range_type == "LIFETIME":
        return _LIFETIME_START, today
    return today - timedelta(days=_PAST[range_type] - 1), today


def expand_accounts(
    session: Session,
    *,
    tenant_id: UUID,
    scope: AccountScope,
    optimizer_scope: RuleBindingScope | None = None,
) -> dict[str, AccountInfo]:
    """展开租户内可读账户；同一账户属于多个 BC 时只取一次（最小 BC ID，结果稳定）。"""
    statement = usable_grants(
        tenant_id=tenant_id, bc_id=col(BCAccountAccess.bc_id), action="read"
    )
    if scope.mode == "BC":
        statement = statement.where(col(BCAccountAccess.bc_id).in_(scope.bc_ids))
    elif scope.mode == "ACCOUNTS":
        statement = statement.where(
            col(BCAccountAccess.advertiser_id).in_(scope.advertiser_ids)
        )
    if optimizer_scope is not None:
        # The optimizer boundary is an intersection, never a replacement for
        # the rule scope.  Existing/manual ad objects in these accounts remain
        # visible because only account grants are narrowed here.
        statement = statement.where(
            col(BCAccountAccess.bc_id) == optimizer_scope.bc_id,
            col(BCAccountAccess.advertiser_id).in_(optimizer_scope.advertiser_ids),
        )
    statement = statement.order_by(
        col(BCAccountAccess.advertiser_id), col(BCAccountAccess.bc_id)
    )
    bc_by_account: dict[str, str] = {}
    for grant in session.exec(statement).all():
        bc_by_account.setdefault(grant.advertiser_id, grant.bc_id)
    if not bc_by_account:
        return {}
    rows = session.exec(
        select(AdvertiserAccount).where(
            col(AdvertiserAccount.tenant_id) == tenant_id,
            col(AdvertiserAccount.advertiser_id).in_(list(bc_by_account)),
        )
    ).all()
    return {
        row.advertiser_id: AccountInfo(
            row.advertiser_id,
            bc_by_account[row.advertiser_id],
            row.timezone,
            row.currency,
            row.name,
        )
        for row in sorted(rows, key=lambda r: r.advertiser_id)
    }


def plan_next_run(
    session: Session,
    *,
    tenant_id: UUID,
    definition: RuleDefinition,
    rule_timezone: str,
    after: datetime,
) -> datetime | None:
    """规则的下一次运行时刻。按账户时区时取规则范围内全部账户的时区，最早的槽位先运行；
    暂时没有账户时仍按规则时区排期，账户接入后的下一次排期自动纳入。"""
    zones: set[str] = {rule_timezone}
    if definition.schedule.timezone_basis == "ACCOUNT":
        accounts = expand_accounts(
            session, tenant_id=tenant_id, scope=definition.accounts
        )
        zones = {info.timezone for info in accounts.values()} or zones
    return next_run_at(definition.schedule, after=after, timezones=zones)


def _fresh_accounts(
    session: Session,
    *,
    tenant_id: UUID,
    accounts: dict[str, AccountInfo],
    kind: str,
    now: datetime,
) -> tuple[dict[str, AccountInfo], list[dict[str, str]]]:
    """以账户本地「今天」的同步状态判断新鲜度；本地零点后不久今天尚未同步时看昨天。"""
    if not accounts:
        return {}, []
    rows = session.connection().execute(
        text(
            "SELECT advertiser_id, stat_date, last_success_at FROM report_sync_state "
            "WHERE tenant_id = :t AND report_kind = :k AND status='COMPLETE' AND advertiser_id = ANY(:advs) "
            "AND stat_date >= :since"
        ),
        {
            "t": tenant_id,
            "k": kind,
            "advs": list(accounts),
            "since": now.date() - timedelta(days=2),
        },
    )
    success: dict[str, dict[date, datetime | None]] = defaultdict(dict)
    for row in rows:
        success[row.advertiser_id][row.stat_date] = row.last_success_at
    fresh, skipped = {}, []
    for adv, info in accounts.items():
        latest = fresh_report_success(
            success[adv],
            timezone=info.timezone,
            kind=kind,
            now=now,
        )
        if latest is not None:
            fresh[adv] = info
        else:
            skipped.append({"advertiser_id": adv, "reason": "STALE_REPORT"})
    return fresh, skipped


def _freshness_metadata(
    session: Session,
    *,
    tenant_id: UUID,
    accounts: dict[str, AccountInfo],
    report_kinds: tuple[str, ...],
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Capture per-account stat date, timezone, source and publication watermark."""
    if not accounts:
        return [], {}
    rows = (
        session.connection()
        .execute(
            text(
                "SELECT advertiser_id, report_kind, stat_date, status, last_success_at, "
                "observed_at, publication_version FROM report_sync_state "
                "WHERE tenant_id=:t AND advertiser_id = ANY(:advs) "
                "AND report_kind = ANY(:kinds) AND stat_date >= :since "
                "ORDER BY advertiser_id, report_kind, stat_date"
            ),
            {
                "t": tenant_id,
                "advs": list(accounts),
                "kinds": list(report_kinds),
                "since": now.date() - timedelta(days=31),
            },
        )
        .all()
    )
    freshness: list[dict[str, Any]] = []
    versions: dict[str, int] = {}
    for row in rows:
        info = accounts.get(row.advertiser_id)
        if info is None:
            continue
        key = f"{row.advertiser_id}:{row.report_kind}:{row.stat_date.isoformat()}"
        versions[key] = int(row.publication_version or 0)
        freshness.append(
            {
                "advertiser_id": row.advertiser_id,
                "bc_id": info.bc_id,
                "report_kind": row.report_kind,
                "source": "report_sync_state",
                "stat_date": row.stat_date.isoformat(),
                "timezone": info.timezone,
                "status": row.status,
                "last_success_at": row.last_success_at.isoformat()
                if row.last_success_at
                else None,
                "observed_at": row.observed_at.isoformat() if row.observed_at else None,
                "publication_version": int(row.publication_version or 0),
            }
        )
    return freshness, versions


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation, ValueError:
        return None
    return result if result.is_finite() else None


def _first_number(config: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        value = _decimal(config.get(key))
        if value:
            return value
    return None


def _created_date(value: Any, timezone: str) -> date | None:
    """目录 create_time：数字为 epoch 秒，字符串按平台 UTC 时间；按账户时区取本地日。"""
    moment: datetime | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.fromtimestamp(value, UTC)
    elif isinstance(value, str) and value.strip():
        raw = value.strip().replace("T", " ").removesuffix("Z")
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(ZoneInfo(timezone)).date() if moment else None


def _projections(
    session: Session, tenant_id: UUID, advertisers: list[str]
) -> dict[tuple[str, str], Any]:
    rows = session.connection().execute(
        text(
            "SELECT DISTINCT ON (advertiser_id, campaign_remote_id) advertiser_id, "
            "campaign_remote_id, provider_label, drama_name, status "
            "FROM campaign_name_projection WHERE tenant_id = :t AND advertiser_id = ANY(:advs) "
            "ORDER BY advertiser_id, campaign_remote_id, grouping_revision DESC, "
            "name_revision DESC, observed_at DESC"
        ),
        {"t": tenant_id, "advs": advertisers},
    )
    return {(r.advertiser_id, r.campaign_remote_id): r for r in rows}


def _objects(
    session: Session,
    tenant_id: UUID,
    accounts: dict[str, AccountInfo],
    now: datetime,
) -> dict[str, Entity]:
    advertisers = list(accounts)
    # 规则只需创建时间、预算和出价；目录完整创意正文可达数百 MB，
    # 在数据库内投影所需键，避免多条规则并行时重复解码无用配置挤爆报表 Worker。
    config_fields = ", ".join(
        f"'{key}', configuration->'{key}'"
        for key in ("create_time", *_BUDGET_KEYS, *_ROAS_KEYS)
    )
    rows = (
        session.connection()
        .execute(
            text(
                "SELECT advertiser_id, kind, remote_id, parent_remote_id, name, "
                f"operation_status, jsonb_build_object({config_fields}) AS configuration FROM ad_object "
                "WHERE tenant_id = :t AND advertiser_id = ANY(:advs) "
                "AND kind IN ('campaign','adgroup','ad')"
            ),
            {"t": tenant_id, "advs": advertisers},
        )
        .all()
    )
    projections = _projections(session, tenant_id, advertisers)
    by_key = {(r.advertiser_id, r.kind, r.remote_id): r for r in rows}

    def campaign_of(row: Any) -> str | None:
        # 广告组、广告沿上级链找到所属系列，剧名只来自系列名。
        while row is not None and row.kind != "campaign":
            row = by_key.get(
                (row.advertiser_id, _PARENT[row.kind], row.parent_remote_id)
            )
        return row.remote_id if row is not None else None

    entities: dict[str, Entity] = {}
    for row in rows:
        info = accounts[row.advertiser_id]
        config = row.configuration or {}
        campaign = campaign_of(row)
        projection = (
            projections.get((row.advertiser_id, campaign)) if campaign else None
        )
        valid = projection is not None and projection.status == "VALID"
        created = _created_date(config.get("create_time"), info.timezone)
        days = (
            Decimal((local_today(info.timezone, now) - created).days)
            if created
            else None
        )
        parent_kind = _PARENT.get(row.kind)
        entities[f"{row.kind}:{row.advertiser_id}:{row.remote_id}"] = Entity(
            key=f"{row.kind}:{row.advertiser_id}:{row.remote_id}",
            kind=row.kind,
            advertiser_id=row.advertiser_id,
            bc_id=info.bc_id,
            remote_id=row.remote_id,
            name=row.name or row.remote_id,
            drama=f"{projection.provider_label}-{projection.drama_name}"
            if valid and projection is not None
            else None,
            naming_valid=valid,
            status=row.operation_status,
            currency=info.currency,
            timezone=info.timezone,
            parent_key=f"{parent_kind}:{row.advertiser_id}:{row.parent_remote_id}"
            if parent_kind and row.parent_remote_id
            else None,
            attributes={
                **hierarchy_statuses(row, by_key),
                "name": row.name or row.remote_id,
                "budget": _first_number(config, _BUDGET_KEYS),
                "roas_bid": _first_number(config, _ROAS_KEYS),
                "days_since_creation": days,
            },
        )
    return entities


def _materials(
    session: Session,
    tenant_id: UUID,
    accounts: dict[str, AccountInfo],
    ads: dict[str, Entity],
    *,
    definition: RuleDefinition | None = None,
    now: datetime | None = None,
) -> dict[str, Entity]:
    # 延迟导入避免 Entity 数据合同与素材加载器循环导入。
    from app.modules.automation_rules.material_inputs import load_material_entities

    return load_material_entities(
        session,
        tenant_id,
        accounts,
        ads,
        definition=definition,
        now=now,
    )


def _add(
    target: dict[str, Decimal | None], row: Any, *, strict_facts: bool = False
) -> None:
    for key in STORED_KEYS:
        value = getattr(row, key)
        if strict_facts and (value is None or (key in target and target[key] is None)):
            target[key] = None
        elif value is not None:
            target[key] = (target.get(key) or Decimal(0)) + value
        else:
            target.setdefault(key, None)


def _facts(
    session: Session,
    tenant_id: UUID,
    accounts: dict[str, AccountInfo],
    range_type: str,
    *,
    material: bool,
    now: datetime,
    strict_facts: bool = False,
) -> dict[str, dict[str, Decimal | None]]:
    windows = [local_window(range_type, accounts[a].timezone, now) for a in accounts]
    # 反向冲突重算要求完整事实：跨天的部分 NULL 不能被 SUM 静默忽略。
    # 普通规则继续沿用已有求和口径，此参数只决定重算证据是否足够。
    sums = ", ".join(
        (
            f"CASE WHEN count(f.{key}) = count(*) THEN sum(f.{key}) END AS {key}"
            if strict_facts
            else f"sum(f.{key}) AS {key}"
        )
        for key in STORED_KEYS
    )
    table, ids = (
        ("creative_report_day", "f.creative_id")
        if material
        else ("ad_report_day", "f.campaign_id, f.adgroup_id, f.ad_id")
    )
    rows = (
        session.connection()
        .execute(
            text(
                f"SELECT f.advertiser_id, {ids}, {sums} FROM {table} f "
                "JOIN unnest(CAST(:advs AS text[]), CAST(:starts AS date[]), CAST(:ends AS date[])) "
                "AS w(adv, s, e) ON w.adv = f.advertiser_id AND f.stat_date BETWEEN w.s AND w.e "
                f"WHERE f.tenant_id = :t GROUP BY f.advertiser_id, {ids}"
            ),
            {
                "t": tenant_id,
                "advs": list(accounts),
                "starts": [w[0] for w in windows],
                "ends": [w[1] for w in windows],
            },
        )
        .all()
    )
    result: dict[str, dict[str, Decimal | None]] = defaultdict(dict)
    for row in rows:
        adv = row.advertiser_id
        if material:
            _add(
                result[f"creative:{adv}:{row.creative_id}"],
                row,
                strict_facts=strict_facts,
            )
            continue
        # 广告日报是唯一明细来源，向上累加到广告组、系列。
        for key in (
            f"ad:{adv}:{row.ad_id}",
            f"adgroup:{adv}:{row.adgroup_id}",
            f"campaign:{adv}:{row.campaign_id}",
        ):
            _add(result[key], row, strict_facts=strict_facts)
    return dict(result)


def load_inputs(
    session: Session,
    *,
    tenant_id: UUID,
    definition: RuleDefinition,
    now: datetime,
    slot_at: datetime | None = None,
    strict_facts: bool = False,
    optimizer_scope: RuleBindingScope | None = None,
) -> Inputs:
    """slot_at 为定时运行的槽位：按账户时区运行时只判断当地正处于该槽位的账户；
    试运行等不带槽位的判断覆盖全部账户。"""
    accounts = expand_accounts(
        session,
        tenant_id=tenant_id,
        scope=definition.accounts,
        optimizer_scope=optimizer_scope,
    )
    if slot_at is not None and definition.schedule.timezone_basis == "ACCOUNT":
        accounts = {
            adv: info
            for adv, info in accounts.items()
            if due_at(definition.schedule, slot_at=slot_at, timezone=info.timezone)
        }
    level = definition.apply_to.level
    kind = "creative_day" if level == "material" else "ad_day"
    freshness, publication_versions = _freshness_metadata(
        session,
        tenant_id=tenant_id,
        accounts=accounts,
        report_kinds=(kind, "ad_day") if kind != "ad_day" else (kind,),
        now=now,
    )
    accounts, skipped = _fresh_accounts(
        session, tenant_id=tenant_id, accounts=accounts, kind=kind, now=now
    )
    if level == "material" and accounts:
        accounts, ledger_skipped = _fresh_accounts(
            session, tenant_id=tenant_id, accounts=accounts, kind="ad_day", now=now
        )
        skipped.extend(ledger_skipped)
    if not accounts:
        return Inputs(
            accounts={},
            entities={},
            facts={},
            skipped_accounts=skipped,
            freshness=freshness,
            publication_versions=publication_versions,
        )
    ranges: list[str] = sorted(
        {
            str(c.range)
            for c in definition.conditions
            if c.range and RULE_METRICS[c.metric].kind == "fact"
        }
    )
    proof = {}
    if level == "material":
        from app.modules.automation_rules.material_source import source_proof

        session.flush()
        proof = source_proof(session, tenant_id, accounts, ranges, now)
    entities = _objects(session, tenant_id, accounts, now)
    if "material" in (level, definition.action.level):
        entities.update(
            _materials(
                session,
                tenant_id,
                accounts,
                entities,
                definition=definition if level == "material" else None,
                now=now,
            )
        )
    facts: dict[str, dict[str, dict[str, Decimal | None]]] = {}
    for range_type in ranges:
        loaded = _facts(
            session,
            tenant_id,
            accounts,
            range_type,
            material=level == "material",
            strict_facts=strict_facts or level == "material",
            now=now,
        )
        if level == "material":
            from app.modules.automation_rules.material_inputs import material_facts

            facts[range_type] = material_facts(entities, loaded)
        else:
            facts[range_type] = {k: v for k, v in loaded.items() if k in entities}
    if level == "material":
        from app.modules.automation_rules.material_inputs import (
            mark_missing_metrics,
            material_issues,
        )

        after = source_proof(session, tenant_id, accounts, ranges, now)
        changed = {adv for adv in accounts if proof.get(adv) != after.get(adv)}
        # 任一窗口/身份变化后不能保留先前已通过门控的金额，审计沿用运行缺数字段。
        for key, entity in entities.items():
            if entity.kind == "material" and entity.advertiser_id in changed:
                entities[key] = replace(
                    entity, judgement_issue="MATERIAL_SOURCE_CHANGED"
                )
                for values in facts.values():
                    values.pop(key, None)
        mark_missing_metrics(entities, facts, definition)
        issues = material_issues(entities, definition)
        skipped.extend(issues)
        # 当前使用位置可能在读取途中全部删除，零实体也必须留下来源变化审计。
        audited = {
            item["advertiser_id"]
            for item in issues
            if item["reason"] == "MATERIAL_SOURCE_CHANGED"
        }
        skipped.extend(
            {"advertiser_id": adv, "reason": "MATERIAL_SOURCE_CHANGED", "count": "0"}
            for adv in sorted(changed - audited)
        )
    return Inputs(
        accounts=accounts,
        entities=entities,
        facts=facts,
        skipped_accounts=skipped,
        freshness=freshness,
        publication_versions=publication_versions,
    )
