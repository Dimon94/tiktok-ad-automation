"""从派生桶选择历史帖子名称；页补全与候选筛选共用原名称口径。"""


def material_names_sql(*, page: bool = False) -> str:
    # 源事实同一帖子/日唯一，故不需要额外保存fetched_at：stat_date相同不会
    # 出现两个竞争名称。币种/类型仍按旧事实聚合身份分组。
    bounded = "AND d.row_key=ANY(:row_keys)" if page else ""
    return f"""
        SELECT row_key,min(name COLLATE "C") AS name FROM (
            SELECT DISTINCT ON (d.row_key,d.advertiser_id,d.main_material_type,d.currency,n.key)
                d.row_key,n.value AS name
            FROM material_entity_day d CROSS JOIN LATERAL jsonb_each_text(d.post_names) n
            WHERE d.tenant_id=:tenant AND d.advertiser_id=ANY(:advs)
                AND d.stat_date BETWEEN :start AND :end {bounded}
            ORDER BY d.row_key,d.advertiser_id,d.main_material_type,d.currency,n.key,d.stat_date DESC
        ) latest_post_names GROUP BY row_key
    """
