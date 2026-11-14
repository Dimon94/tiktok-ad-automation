"""现有领域 Schema 的任务能力目录及无写入预检。"""

from uuid import UUID

from pydantic import ValidationError
from sqlmodel import Session, select

from app.modules.automation_rules.schemas import RuleDefinition
from app.modules.strategies.schemas import StrategyConfig


def catalog(version) -> dict:
    from dataclasses import asdict

    from app.modules.ai_workbench.knowledge_refs import sanitize_public
    from app.modules.automation_rules.catalog import RULE_METRICS

    return sanitize_public(
        {
            "version": "1.2.0",
            "proposal_submission_allowed": True,
            "publication_mode": "HUMAN_CONFIRMATION"
            if version.optimization_mode == "MANUAL"
            else "AUTHORIZED_AUTO",
            "strategy": {
                "operations": ["create", "update"],
                "creation": {"name": "required", "config": "complete StrategyConfig"},
                "schema": StrategyConfig.model_json_schema(),
                "effect": "FUTURE_UNFROZEN_BUILDS",
            },
            "rule": {
                "operations": ["create", "update"],
                "creation": {
                    "name": "required",
                    "definition": "complete RuleDefinition; accounts.mode=ACCOUNTS within managed accounts",
                },
                "schema": RuleDefinition.model_json_schema(),
                "metrics": {k: asdict(v) for k, v in RULE_METRICS.items()},
                "effect": "EXISTING_RULE_EXECUTOR",
            },
            "scope": {"bc_id": version.bc_id, "advertiser_ids": version.advertiser_ids},
            "mode": version.optimization_mode,
            "publication": "Submitting an evidence-backed proposal is allowed in BOTH modes. MANUAL saves it as PENDING for explicit human acceptance; AUTO publishes supported domain changes within managed scope. Archived authorization_limits are not capability flags. Disabled rules may be reviewed; do not enable them without a justified explicit proposal.",
            "unsupported": [
                "scale_copy",
                "automatic_drama_selection",
                "cross_bc",
                "experiments",
                "raw_platform_write",
            ],
            "prevalidation": "Schema and current bindings/authority only; apply rechecks versions, shared usage and all domain constraints.",
        }
    )


def validate_candidate(
    session: Session,
    *,
    context,
    optimizer_id: UUID,
    kind: str,
    after: dict,
    target_id: UUID | None = None,
) -> dict:
    from app.core.errors import DomainError
    from app.modules.ai_workbench.models import (
        AIOptimizerRuleBinding,
        AIOptimizerStrategyBinding,
    )
    from app.modules.ai_workbench.proposals import (
        _strategy_baseline,
        _within_authorization,
    )
    from app.modules.ai_workbench.reviews import authorize, load_optimizer
    from app.modules.automation_rules.models import AutomationRule

    authorize(session, context, write=True)
    _, version = load_optimizer(session, context, optimizer_id)
    try:
        if kind == "strategy":
            config = StrategyConfig.model_validate(after.get("config", after))
            baseline = _strategy_baseline(
                session,
                tenant_id=context.tenant_id,
                optimizer_id=optimizer_id,
                version=version,
            )
            before = {"config": baseline.config}
            shared = (
                session.exec(
                    select(AIOptimizerStrategyBinding).where(
                        AIOptimizerStrategyBinding.tenant_id == context.tenant_id,
                        AIOptimizerStrategyBinding.strategy_version_id == baseline.id,
                        AIOptimizerStrategyBinding.optimizer_id != optimizer_id,
                        AIOptimizerStrategyBinding.active.is_(True),
                    )
                ).first()
                is not None
            )
            normalized = {"config": config.model_dump(mode="json")}
        elif kind == "rule":
            definition = RuleDefinition.model_validate(after.get("definition", after))
            rule = session.exec(
                select(AutomationRule).where(
                    AutomationRule.tenant_id == context.tenant_id,
                    AutomationRule.id == target_id,
                )
            ).one_or_none()
            binding = session.exec(
                select(AIOptimizerRuleBinding).where(
                    AIOptimizerRuleBinding.tenant_id == context.tenant_id,
                    AIOptimizerRuleBinding.optimizer_id == optimizer_id,
                    AIOptimizerRuleBinding.rule_id == target_id,
                    AIOptimizerRuleBinding.active.is_(True),
                )
            ).first()
            if rule is None or binding is None:
                raise DomainError("ai_rule_not_bound", "规则未绑定")
            shared = (
                session.exec(
                    select(AIOptimizerRuleBinding).where(
                        AIOptimizerRuleBinding.tenant_id == context.tenant_id,
                        AIOptimizerRuleBinding.optimizer_id != optimizer_id,
                        AIOptimizerRuleBinding.rule_id == target_id,
                        AIOptimizerRuleBinding.active.is_(True),
                    )
                ).first()
                is not None
            )
            before = {"definition": rule.definition}
            normalized = {"definition": definition.model_dump(mode="json")}
        else:
            raise DomainError("ai_capability_unsupported", "不支持此动作")
        allowed, reason = _within_authorization(
            version=version, kind=kind, before=before, after=normalized
        )
        return {
            "valid": True,
            "within_authorization": allowed and not shared,
            "requires_confirmation": version.optimization_mode != "AUTO"
            or not allowed
            or shared,
            "shared_configuration": shared,
            "reason": "shared_configuration_requires_copy" if shared else reason,
            "normalized": normalized,
            "writes_performed": False,
        }
    except (ValidationError, DomainError) as error:
        return {
            "valid": False,
            "reason": getattr(error, "code", "schema_invalid"),
            "writes_performed": False,
        }
