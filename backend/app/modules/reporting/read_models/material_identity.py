"""以当前页身份反向定位历史帖子，不枚举整个日期窗口的所有素材事实。"""

import json
from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

from app.modules.reporting import query_diagnostics


def hydrate_material_identities(
    session: Session,
    *,
    tenant_id: UUID,
    advertisers: Sequence[str],
    row_keys: Sequence[str],
    start: date,
    end: date,
) -> dict[tuple[str, str, str], str]:
    keys = [json.loads(value) for value in row_keys]
    videos = sorted({key[1] for key in keys if key[0] == "material"})
    posts = sorted(
        {(key[1], key[2]) for key in keys if key[0] == "post" and key[1] in advertisers}
    )
    if not advertisers or not (videos or posts):
        return {}
    rows = session.connection().execute(
        text("""
        WITH candidates AS MATERIALIZED (
            SELECT DISTINCT l.advertiser_id AS adv, l.main_material_id AS mid,
                l.main_material_type AS mtype
            FROM unnest(CAST(:videos AS text[])) v(vid)
            JOIN material_post_link l ON l.tenant_id=:tenant
                AND l.platform_material_id=v.vid AND l.advertiser_id=ANY(:advertisers)
            WHERE l.main_material_type IN ('VIDEO_SPARK_ADS','VIDEO_NON_SPARK_ADS')
            UNION
            SELECT l.advertiser_id, l.main_material_id, l.main_material_type
            FROM unnest(CAST(:post_advertisers AS text[]), CAST(:posts AS text[])) p(adv,mid)
            JOIN material_post_link l ON l.tenant_id=:tenant
                AND l.advertiser_id=p.adv AND l.main_material_id=p.mid
            WHERE l.main_material_type IN ('VIDEO_SPARK_ADS','VIDEO_NON_SPARK_ADS')
        )
        SELECT c.adv, c.mid, c.mtype, chosen.vid
        FROM candidates c
        CROSS JOIN LATERAL (
            SELECT l.platform_material_id AS vid FROM material_post_link l
            WHERE l.tenant_id=:tenant AND l.advertiser_id=c.adv
                AND l.main_material_id=c.mid AND l.main_material_type=c.mtype
                AND l.platform_material_id IS NOT NULL
            ORDER BY l.platform_material_id COLLATE "C" LIMIT 1
        ) chosen
        WHERE (chosen.vid=ANY(:videos) OR EXISTS (
            SELECT 1 FROM unnest(CAST(:post_advertisers AS text[]),CAST(:posts AS text[])) p(adv,mid)
            WHERE p.adv=c.adv AND p.mid=c.mid
        )) AND EXISTS (
            SELECT 1 FROM material_report_day f
            WHERE f.tenant_id=:tenant AND f.advertiser_id=c.adv
                AND f.main_material_id=c.mid AND f.main_material_type=c.mtype
                AND f.stat_date BETWEEN :start AND :end
        )
    """),
        {
            "tenant": tenant_id,
            "advertisers": sorted(set(advertisers)),
            "videos": videos,
            "post_advertisers": [adv for adv, _ in posts],
            "posts": [mid for _, mid in posts],
            "start": start,
            "end": end,
        },
    )
    result = {(row.adv, row.mid, row.mtype): row.vid for row in rows}
    query_diagnostics.count("material_posts", len(result))
    return result
