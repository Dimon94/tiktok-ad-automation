"""读投影触发器 SQL 模板；迁移需保存生成后的固定 SQL，不运行时引用模型。"""

from app.modules.reporting.read_models.sql_values import (
    _created_date,
    _first_truthy_num,
    _json_num,
)

DIRECTORY_BUCKET = "DATE '1970-01-01'"


def _bump(keys: str, *, complete: bool = False) -> str:
    derived = "1" if complete else "0"
    update = ",derived_revision=read_bucket_state.source_revision+1" if complete else ""
    return f"""
        INSERT INTO read_bucket_state
            (tenant_id,advertiser_id,kind,bucket,source_revision,derived_revision,observed_at)
        SELECT keys.tenant_id,keys.advertiser_id,keys.kind,keys.bucket,1,{derived},clock_timestamp()
        FROM ({keys}) keys JOIN advertiser_account account
            ON account.tenant_id=keys.tenant_id AND account.advertiser_id=keys.advertiser_id
        ORDER BY keys.tenant_id,keys.advertiser_id,keys.kind,keys.bucket
        ON CONFLICT (tenant_id,advertiser_id,kind,bucket) DO UPDATE SET
            source_revision=read_bucket_state.source_revision+1,
            observed_at=clock_timestamp(){update};
    """


def _replan(statement: str) -> str:
    # transition表每次statement大小可从1变为数万；PL/pgSQL静态计划会沿用首批估计。
    # 只重新规划已证明昂贵的固定内部DML，不拼接调用者数据、不改变原子事务。
    return f"EXECUTE $qrm_transition${statement}$qrm_transition$;"


def entity_refresh(
    keys: str, *, source_rows: str | None = None, delete_only: bool = False
) -> str:
    """keys精确到对象身份；发布一次statement只重建该statement触及的对象。"""
    fields = "tenant_id,advertiser_id,kind,remote_id,parent_kind,parent_remote_id,name,ad_type,operation_status,review_status,delivery_status,budget_mode,optimization_goal,budget_value,target_roas,roas_bid,created_local_date,observed_at,published_version"
    budget = _first_truthy_num(
        "o.configuration", ("budget", "daily_budget", "campaign_daily_budget")
    )
    target = _first_truthy_num("o.configuration", ("roas_bid", "target_roas"))
    strict = _json_num("(o.configuration->'roas_bid')")
    created = "qrm_created_local_date(o.configuration,a.timezone)"
    deletion = f"""
        DELETE FROM ad_entity_read target USING ({keys}) k
        WHERE target.tenant_id=k.tenant_id AND target.advertiser_id=k.advertiser_id
            AND target.kind=k.kind AND target.remote_id=k.remote_id;
    """
    if delete_only:
        return deletion
    # statement transition rows已是canonical经CAS真正写入的行，不再根据keys
    # 重读宽表。避免空库/旧统计让优化器逐key按账户索引扫描全部目录。
    source = (
        f"{source_rows} o"
        if source_rows
        else f"""({keys}) k JOIN ad_object o
        ON o.tenant_id=k.tenant_id AND o.advertiser_id=k.advertiser_id
            AND o.kind=k.kind AND o.remote_id=k.remote_id"""
    )
    return (
        deletion
        + f"""
        INSERT INTO ad_entity_read ({fields})
        SELECT o.tenant_id,o.advertiser_id,o.kind,o.remote_id,o.parent_kind,o.parent_remote_id,
            o.name,o.ad_type,o.operation_status,o.review_status,o.delivery_status,
            o.configuration->>'budget_mode',o.configuration->>'optimization_goal',
            {budget},{target},{strict},{created},o.observed_at,o.published_version
        FROM {source}
        JOIN advertiser_account a ON a.tenant_id=o.tenant_id AND a.advertiser_id=o.advertiser_id;
    """
    )


def naming_refresh(keys: str) -> str:
    fields = "tenant_id,advertiser_id,campaign_remote_id,raw_name,provider_label,drama_name,status,parser_revision,name_revision,grouping_revision,observed_at"
    return f"""
        DELETE FROM campaign_name_current target USING ({keys}) k
        WHERE target.tenant_id=k.tenant_id AND target.advertiser_id=k.advertiser_id
            AND target.campaign_remote_id=k.campaign_remote_id;
        INSERT INTO campaign_name_current ({fields})
        SELECT DISTINCT ON (p.tenant_id,p.advertiser_id,p.campaign_remote_id)
            {",".join("p." + f for f in fields.split(","))}
        FROM ({keys}) k JOIN campaign_name_projection p ON p.tenant_id=k.tenant_id
            AND p.advertiser_id=k.advertiser_id AND p.campaign_remote_id=k.campaign_remote_id
        ORDER BY p.tenant_id,p.advertiser_id,p.campaign_remote_id,
            p.grouping_revision DESC,p.name_revision DESC,p.observed_at DESC;
    """


def material_refresh(keys: str) -> str:
    # 每个使用身份独立重建全部历史候选。局部窗口阻止LATERAL被拉平成全表JOIN，
    # 精确身份探测与每批重规划配合，避免前批基数让后批重复扫描无关引用。
    # 精确ID优先后再取最大完整版本，所有同版本候选保留，不能用LIMIT 1猜消歧。
    fields = "id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,ad_material_id,material_type,name,operation_status,complete,published_version"
    deletion = f"""
        DELETE FROM current_ad_material target USING ({keys}) k
        WHERE target.tenant_id=k.tenant_id AND target.advertiser_id=k.advertiser_id
            AND target.ad_remote_id=k.ad_remote_id AND target.platform_material_id=k.platform_material_id
            AND target.material_type=k.material_type;
    """
    insertion = f"""
        INSERT INTO current_ad_material ({fields})
        SELECT {",".join("chosen." + field for field in fields.split(","))}
        FROM ({keys}) k CROSS JOIN LATERAL (
            SELECT {",".join("preferred." + field for field in fields.split(","))}
            FROM (
                SELECT candidates.*,max(published_version) OVER () AS newest
                FROM (
                    SELECT u.*,bool_or(u.ad_material_id IS NOT NULL) OVER () AS has_precise
                    FROM ad_material_reference u
                    WHERE u.tenant_id=k.tenant_id AND u.advertiser_id=k.advertiser_id
                        AND u.ad_remote_id=k.ad_remote_id
                        AND u.platform_material_id=k.platform_material_id
                        AND u.material_type=k.material_type AND u.complete
                ) candidates
                WHERE NOT has_precise OR ad_material_id IS NOT NULL
            ) preferred
            WHERE published_version=newest
        ) chosen;

    """

    return _replan(deletion) + _replan(insertion)


def trigger_statements() -> list[str]:
    # 每对象扫描pg_timezone_names会显著放大目录发布。直接按需解析，缺值不查
    # timezone catalog；不合法平台日期/时区返回未知，不阻止canonical事实写入。
    parsed_date = _created_date("cfg", "zone")
    result = [
        f"""CREATE FUNCTION qrm_created_local_date(cfg jsonb,zone text) RETURNS date
        LANGUAGE plpgsql STABLE AS $$ BEGIN RETURN {parsed_date};
        EXCEPTION WHEN invalid_parameter_value OR invalid_datetime_format OR datetime_field_overflow OR numeric_value_out_of_range
        THEN RETURN NULL; END $$""",
        """CREATE INDEX ix_material_post_link_video_page ON material_post_link
        (tenant_id,platform_material_id,advertiser_id,main_material_id,main_material_type)
        WHERE platform_material_id IS NOT NULL""",
    ]
    sources = {
        "ad_object": (
            "tenant_id,advertiser_id,kind,remote_id",
            "directory",
            entity_refresh,
        ),
        "campaign_name_projection": (
            "tenant_id,advertiser_id,campaign_remote_id",
            "naming",
            naming_refresh,
        ),
        "ad_material_reference": (
            "tenant_id,advertiser_id,ad_remote_id,platform_material_id,material_type",
            "usage",
            material_refresh,
        ),
        "ad_report_day": ("tenant_id,advertiser_id,stat_date", "ad_day", None),
        "material_report_day": (
            "tenant_id,advertiser_id,stat_date",
            "material_day",
            None,
        ),
        "material_post_link": (
            "tenant_id,advertiser_id,main_material_id",
            "link",
            None,
        ),
        "report_sync_state": (
            "tenant_id,advertiser_id,report_kind,stat_date",
            "state",
            None,
        ),
    }
    for table, (fields, kind, refresh) in sources.items():
        for operation in ("INSERT", "UPDATE", "DELETE"):
            relations = (
                ("new_rows",)
                if operation == "INSERT"
                else ("old_rows",)
                if operation == "DELETE"
                else ("old_rows", "new_rows")
            )
            keys = " UNION ".join(
                f"SELECT DISTINCT {fields} FROM {name}" for name in relations
            )
            # INSERT ... ON CONFLICT 即使没有更新任何行，也会调用 UPDATE statement
            # trigger。空transition表不仅无工作，首次还可能缓存对后继大批次极差的
            # 执行计划；在运行refresh SQL之前短路，避免由空UPSERT分支污染计划。
            present = " UNION ALL ".join(f"SELECT 1 FROM {name}" for name in relations)
            body = f"IF NOT EXISTS({present}) THEN RETURN NULL; END IF;"
            if refresh:
                body += _bump(
                    f"SELECT DISTINCT tenant_id,advertiser_id,'{kind}'::varchar AS kind,{DIRECTORY_BUCKET} AS bucket FROM ({keys}) k",
                    complete=True,
                )
                if table == "ad_object":
                    body += entity_refresh(
                        keys,
                        source_rows="new_rows" if operation != "DELETE" else None,
                        delete_only=operation == "DELETE",
                    )
                else:
                    body += refresh(keys)
            elif kind in {"ad_day", "material_day"}:
                body += _bump(
                    f"SELECT tenant_id,advertiser_id,'{kind}'::varchar AS kind,stat_date AS bucket FROM ({keys}) k"
                )
            elif kind == "state":
                body += _bump(
                    f"SELECT tenant_id,advertiser_id,report_kind AS kind,stat_date AS bucket FROM ({keys}) k"
                )
            else:
                body += _bump(
                    f"SELECT DISTINCT tenant_id,advertiser_id,'link'::varchar AS kind,{DIRECTORY_BUCKET} AS bucket FROM ({keys}) k",
                    complete=True,
                )
                body += _replan(
                    _bump(f"""SELECT DISTINCT f.tenant_id,f.advertiser_id,'material_day'::varchar AS kind,f.stat_date AS bucket
                    FROM ({keys}) k JOIN material_report_day f ON f.tenant_id=k.tenant_id
                        AND f.advertiser_id=k.advertiser_id AND f.main_material_id=k.main_material_id""")
                )
            suffix = f"{table}_{operation.lower()}"
            result.append(
                f"CREATE FUNCTION qrm_{suffix}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN {body} RETURN NULL; END $$"
            )
            referencing = " ".join(
                (
                    "OLD TABLE AS old_rows"
                    if name == "old_rows"
                    else "NEW TABLE AS new_rows"
                )
                for name in relations
            )
            result.append(
                f"CREATE TRIGGER qrm_{suffix} AFTER {operation} ON {table} REFERENCING {referencing} FOR EACH STATEMENT EXECUTE FUNCTION qrm_{suffix}()"
            )
    changed_accounts = """SELECT n.tenant_id,n.advertiser_id FROM new_rows n
        JOIN old_rows o USING (tenant_id,advertiser_id)
        WHERE (n.name,n.currency,n.timezone,n.remote_status) IS DISTINCT FROM
              (o.name,o.currency,o.timezone,o.remote_status)"""
    changed_zones = """SELECT obj.tenant_id,obj.advertiser_id,obj.kind,obj.remote_id
        FROM new_rows n JOIN old_rows o USING (tenant_id,advertiser_id)
        JOIN ad_object obj ON obj.tenant_id=n.tenant_id AND obj.advertiser_id=n.advertiser_id
        WHERE n.timezone IS DISTINCT FROM o.timezone"""
    body = (
        f"IF NOT EXISTS({changed_accounts}) THEN RETURN NULL; END IF;"
        + _bump(
            f"SELECT tenant_id,advertiser_id,'directory'::varchar AS kind,{DIRECTORY_BUCKET} AS bucket FROM ({changed_accounts}) k",
            complete=True,
        )
        + f"IF EXISTS({changed_zones}) THEN {entity_refresh(changed_zones)} END IF;"
    )
    result.extend(
        [
            f"CREATE FUNCTION qrm_account_update() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN {body} RETURN NULL; END $$",
            "CREATE TRIGGER qrm_account_update AFTER UPDATE ON advertiser_account REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION qrm_account_update()",
        ]
    )
    return result
