"""按当前查询范围一次汇总实际可执行的组和广告，避免逐组合重扫。"""

from .ad_readiness import included_ad_sql

# scope 由调用方冻结租户、预览及组合范围。物化一次范围和结果，使缺少统计
# 信息时也不会把广告、排除事实放到逐 unit 的相关聚合中反复扫描。
# 先排除广告，再统计有剩余广告的组；VIDEO/历史记录没有排除事实时全部计入。
INCLUDED_OBJECT_COUNTS = (
    """,
 included_unit_scope AS MATERIALIZED (
  SELECT DISTINCT tenant_id,preview_id,id FROM scope WHERE included
 ), included_object_counts AS MATERIALIZED (
  SELECT u.tenant_id,u.preview_id,u.id unit_id,
   count(DISTINCT g.id) group_count,count(a.id) ad_count
  FROM included_unit_scope u
  JOIN planned_group g ON g.tenant_id=u.tenant_id AND g.preview_id=u.preview_id AND g.unit_id=u.id
  JOIN planned_ad a ON a.tenant_id=g.tenant_id AND a.preview_id=g.preview_id AND a.group_id=g.id
  WHERE """
    + included_ad_sql()
    + """ GROUP BY u.tenant_id,u.preview_id,u.id
 )
"""
)
