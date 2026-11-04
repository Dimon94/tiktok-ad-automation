from celery import Celery  # type: ignore[import-untyped]
from kombu import Queue  # type: ignore[import-untyped]

from app.core.config import settings

celery_app = Celery("tiktok_ads", broker=settings.REDIS_URL)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    result_backend=None,
    imports=(
        "app.jobs.tasks",
        "app.modules.material_production.tasks",
        "app.modules.accounts.tasks",
        "app.modules.accounts.capability_tasks",
        "app.modules.accounts.refresh_tasks",
        "app.modules.accounts.runtime_directory_tasks",
        "app.modules.accounts.discovery_recovery",
        "app.modules.providers.tasks",
        "app.modules.materials.tasks",
        "app.modules.materials.cover_tasks",
        "app.modules.materials.source_cover_tasks",
        "app.modules.materials.validation_tasks",
        "app.modules.materials.cleanup_tasks",
        "app.modules.materials.ingest_tasks",
        "app.modules.materials.push_tasks",
        "app.modules.builds.draft_tasks",
        "app.modules.builds.image_composition_tasks",
        "app.modules.builds.image_candidate_tasks",
        "app.modules.builds.carousel_music_tasks",
        "app.modules.builds.preview_tasks",
        "app.modules.builds.submission_tasks",
        "app.modules.builds.tasks",
        "app.modules.builds.scene_tasks",
        "app.modules.builds.recovery_tasks",
        # A7 durable directory/report runs register before the outbox publisher
        # starts, so a recovery message can never hit an unknown task/queue.
        "app.modules.ads.tasks",
        "app.modules.reporting.job_tasks",
        "app.modules.reporting.health",
        "app.modules.reporting.balance_tasks",
        "app.modules.reporting.read_models.tasks",
        "app.modules.sync_control.tasks",
        "app.modules.sync_control.archive_tasks",
        "app.modules.reporting.export_tasks",
        "app.modules.ad_management.tasks",
        "app.modules.ad_management.submissions",
        "app.modules.automation_rules.tasks",
        "app.modules.business.tasks",
    ),
    task_queues=(
        Queue("resources"),
        Queue("resource-results"),
        Queue("builds"),
        Queue("control"),
        Queue("ads-directory"),
        Queue("ads-publish"),
        Queue("ads-reporting"),
        Queue("ads-backfill"),
        Queue("ads-live-directory"),
        Queue("ad-management"),
    ),
    # 消费者按部署角色隔离准备与结果；多队列角色仍公平轮询，不扩大外部额度。
    broker_transport_options={"queue_order_strategy": "round_robin"},
    task_default_queue="control",
    task_create_missing_queues=False,
    task_routes={
        "business.schedule": {"queue": "control"},
        "business.work": {"queue": "ads-reporting"},
        "material_production.provision": {"queue": "control"},
        "material_production.recover": {"queue": "control"},
        "jobs.flush_dispatch": {"queue": "builds", "priority": 0},
        "jobs.compact_dispatches": {"queue": "control"},
        "reporting.plan_due": {"queue": "control"},
        "sync_control.dispatch": {"queue": "control"},
        "sync.archive_runs": {"queue": "control", "priority": 9},
        "reporting.plan_continue": {"queue": "control"},
        "reporting.check_health": {"queue": "control"},
        "reporting.check_bc_health": {"queue": "control"},
        "reporting.job_step": {"queue": "ads-reporting"},
        "reporting.backfill_step": {"queue": "ads-backfill"},
        "reporting.plan_balances": {"queue": "control"},
        "reporting.balance_step": {"queue": "ads-directory"},
        "reporting.project_read_models": {"queue": "ads-reporting"},
        "reporting.cleanup_store": {"queue": "ads-reporting"},
        "ads.sync_step": {"queue": "ads-directory"},
        "ads.publish_step": {"queue": "ads-publish"},
        "ads.targeted_step": {"queue": "ads-live-directory"},
        "reporting.export": {"queue": "ads-reporting"},
        "ad_management.execute": {"queue": "ad-management"},
        "ad_management.reconcile": {"queue": "ad-management"},
        "reporting.cleanup_exports": {"queue": "ads-reporting"},
        "automation.scan_due": {"queue": "control"},
        "automation.run_rule": {"queue": "ads-reporting"},
        "automation.send_feishu": {"queue": "control"},
        "automation.cleanup": {"queue": "control"},
        "automation.scan_summaries": {"queue": "control"},
        "automation.send_summary": {"queue": "control"},
    },
    beat_schedule={
        "schedule-business-accounting": {
            "task": "business.schedule",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "advance-business-accounting": {
            "task": "business.work",
            "schedule": 5.0,
            "options": {"queue": "ads-reporting"},
        },
        # 冷审计最多占一个control进程；过期唤醒不堆积，未配置私有桶时不删除源。
        "archive-terminal-sync-runs": {
            "task": "sync.archive_runs",
            "schedule": 5.0,
            "kwargs": {"max_runs": 5, "time_budget_seconds": 3},
            "options": {"queue": "control", "priority": 9, "expires": 4.0},
        },
        "dispatch-due-sync-heads": {
            "task": "sync_control.dispatch",
            "schedule": 1.0,
            "options": {"queue": "control"},
        },
        # 查询汇总与报表同池；不能在单槽目录publisher后排队超过5秒而被丢弃。
        # 桶级SKIP LOCKED及source_revision保护仍在，不增加Worker或平台调用。
        "project-report-read-models": {
            "task": "reporting.project_read_models",
            "schedule": 5.0,
            "options": {"queue": "ads-reporting"},
        },
        "recover-material-production": {
            "task": "material_production.recover",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-external-imports": {
            "task": "materials.repair_external_imports",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "compact-published-dispatches": {
            "task": "jobs.compact_dispatches",
            "schedule": 60.0,
            "kwargs": {"limit": 5000},
            "options": {"queue": "control"},
        },
        "repair-source-cover-starts": {
            "task": "materials.repair_source_cover_starts",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "scan-abandoned-materials": {
            "task": "materials.scan_abandoned_objects",
            "schedule": 30.0,
            "options": {"queue": "control"},
        },
        "repair-material-ingest-transports": {
            "task": "materials.repair_ingest_transports",
            "schedule": 30.0,
            "options": {"queue": "control"},
        },
        "repair-material-cleanups": {
            "task": "materials.repair_cleanups",
            "schedule": 30.0,
            "options": {"queue": "control"},
        },
        "repair-material-validations": {
            "task": "materials.repair_validations",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        # 封面接续（等待来源、卡住的 UNKNOWN/瞬时失败）直接决定广告能否发出，15 秒一轮。
        "repair-material-covers": {
            "task": "materials.repair_covers",
            "schedule": 15.0,
            "options": {"queue": "control", "expires": 14.0},
        },
        # 目录发现每步只靠一条消息推进；丢失或领取前出错时由这里补派，避免永久“正在发现”。
        "repair-account-discoveries": {
            "task": "accounts.repair_discoveries",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-build-recoveries": {
            "task": "builds.repair_recoveries",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-build-historical-reads": {
            "task": "builds.repair_historical_reads",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-build-scenes": {
            "task": "builds.repair_scenes",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-build-execution": {
            "task": "builds.repair_execution",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        # 素材/封面落定后尽快推进下一片；过期的 tick 直接丢弃，不在积压时叠加。
        "advance-build-execution": {
            "task": "builds.advance_execution",
            "schedule": 5.0,
            "options": {"queue": "control", "expires": 4.0},
        },
        "repair-build-submissions": {
            "task": "builds.repair_submissions",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-account-capabilities": {
            "task": "accounts.repair_capabilities",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-build-previews": {
            "task": "builds.repair_previews",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-material-dispatches": {
            "task": "materials.repair_dispatches",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-carousel-music": {
            "task": "builds.repair_carousel_music",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-image-candidates": {
            "task": "builds.repair_image_candidates",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-image-compositions": {
            "task": "builds.repair_image_compositions",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "repair-draft-preparations": {
            "task": "builds.repair_draft_preparations",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "recover-provider-preparations": {
            "task": "providers.recover_preparations",
            "schedule": 15.0,
            "options": {"queue": "control"},
        },
        # 短投递借用现有搭建槽；不等待控制面的慢规划/修复，不增加Worker或平台额度。
        "flush-dispatch": {
            "task": "jobs.flush_dispatch",
            "schedule": 1.0,
            "options": {"queue": "builds", "priority": 0},
        },
        # Beat only wakes the local durable-plan scanner.  The task checks the
        # explicit feature flag before touching plans or the provider gateway.
        "cleanup-report-store": {
            "task": "reporting.cleanup_store",
            "schedule": 60.0,
            "options": {"queue": "ads-reporting"},
        },
        "scan-account-balances": {
            "task": "reporting.plan_balances",
            "schedule": 1800.0,
            "options": {"queue": "control"},
        },
        "scan-ads-reporting-due": {
            "task": "reporting.plan_due",
            "schedule": 30.0,
            "options": {"queue": "control"},
        },
        "check-sync-health": {
            "task": "reporting.check_health",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "cleanup-report-exports": {
            "task": "reporting.cleanup_exports",
            "schedule": 3600.0,
            "options": {"queue": "ads-reporting"},
        },
        "scan-automation-rules": {
            "task": "automation.scan_due",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "scan-automation-summaries": {
            "task": "automation.scan_summaries",
            "schedule": 60.0,
            "options": {"queue": "control"},
        },
        "cleanup-automation-runs": {
            "task": "automation.cleanup",
            "schedule": 3600.0,
            "options": {"queue": "control"},
        },
    },
)

# 周期消息只负责唤醒持久状态扫描；过期 tick 不应挤占新的业务调度。
# 广告写入 outbox不套周期TTL；V2同步outbox独立遵循持久消息寿命合同。
# 已显式设置的更短周期寿命应保留，不能被默认schedule覆盖。
for periodic_entry in celery_app.conf.beat_schedule.values():
    periodic_entry["options"]["expires"] = min(
        periodic_entry["schedule"],
        periodic_entry["options"].get("expires", periodic_entry["schedule"]),
    )
