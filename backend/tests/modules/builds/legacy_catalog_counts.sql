-- Frozen small-fixture regression oracle from d3a1c1b.

, scope_basis AS (
 SELECT p.id submission_id,u.*, EXISTS (
  SELECT 1 FROM build_submission old JOIN build_unit ou ON ou.tenant_id=old.tenant_id AND ou.preview_id=old.preview_id
  WHERE old.tenant_id=p.tenant_id AND old.draft_id=p.draft_id AND old.ordinal<p.ordinal
  AND ou.drama_id=u.drama_id AND ou.advertiser_id=u.advertiser_id
  AND ou.complete AND ou.readiness IN ('READY','PREPARING')) covered
 FROM page p JOIN build_unit u ON u.tenant_id=p.tenant_id AND u.preview_id=p.preview_id
), scope AS (
 SELECT b.*, b.complete AND b.readiness IN ('READY','PREPARING') AND NOT b.covered included FROM scope_basis b
), totals AS (
 SELECT submission_id,count(DISTINCT drama_id) drama_count,count(DISTINCT advertiser_id) account_count,
 count(*) FILTER(WHERE NOT included) excluded_unit_count,count(*) FILTER(WHERE included) submitted_c,
 coalesce(sum(group_count) FILTER(WHERE included),0) submitted_g,coalesce(sum(ad_count) FILTER(WHERE included),0) submitted_a
 FROM scope GROUP BY submission_id
), material_groups AS (
 SELECT p.id submission_id,g.id group_id,g.unit_id,coalesce(bool_or(ms.status='FAILED'),false) failed
 FROM page p JOIN planned_group g ON g.tenant_id=p.tenant_id AND g.preview_id=p.preview_id
 LEFT JOIN frozen_group_material m ON m.tenant_id=g.tenant_id AND m.preview_id=g.preview_id AND m.drama_id=g.drama_id AND m.group_no=g.group_no
 LEFT JOIN execution_step ms ON ms.tenant_id=g.tenant_id AND ms.submission_id=p.id AND ms.unit_id=g.unit_id AND ms.kind='MATERIAL' AND ms.material_id=m.material_id
 GROUP BY p.id,g.id,g.unit_id
), objects AS (
 SELECT u.submission_id,u.id unit_id,'CAMPAIGN' kind,NULL::uuid group_id,NULL::uuid ad_id FROM scope u WHERE included
 UNION ALL SELECT u.submission_id,u.id,'ADGROUP',g.id,NULL FROM scope u JOIN planned_group g ON g.tenant_id=u.tenant_id AND g.preview_id=u.preview_id AND g.unit_id=u.id WHERE included
 UNION ALL SELECT u.submission_id,u.id,'AD',g.id,a.id FROM scope u JOIN planned_group g ON g.tenant_id=u.tenant_id AND g.preview_id=u.preview_id AND g.unit_id=u.id JOIN planned_ad a ON a.tenant_id=g.tenant_id AND a.preview_id=g.preview_id AND a.group_id=g.id WHERE included
), results AS (
 SELECT o.submission_id,o.kind, CASE
 WHEN nullif(trim(s.remote_id),'') IS NOT NULL THEN 'succeeded'
 WHEN s.status IN ('SUCCEEDED','UNKNOWN') THEN 'unknown'
 WHEN s.status='FAILED' OR EXISTS (SELECT 1 FROM execution_step ancestor WHERE ancestor.tenant_id=:tenant AND ancestor.submission_id=o.submission_id AND ancestor.unit_id=o.unit_id AND ancestor.status='FAILED' AND (ancestor.kind='CTA' OR (ancestor.kind='CAMPAIGN' AND o.kind!='CAMPAIGN') OR (ancestor.kind='ADGROUP' AND o.kind='AD' AND ancestor.group_id=o.group_id)))
 OR (o.kind='CAMPAIGN' AND EXISTS (SELECT 1 FROM material_groups mg WHERE mg.submission_id=o.submission_id AND mg.unit_id=o.unit_id) AND NOT EXISTS (SELECT 1 FROM material_groups mg WHERE mg.submission_id=o.submission_id AND mg.unit_id=o.unit_id AND NOT mg.failed))
 OR (o.kind IN ('ADGROUP','AD') AND EXISTS (SELECT 1 FROM material_groups mg WHERE mg.submission_id=o.submission_id AND mg.group_id=o.group_id AND mg.failed)) THEN 'failed'
 ELSE 'pending' END outcome
 FROM objects o LEFT JOIN execution_step s ON s.tenant_id=:tenant AND s.submission_id=o.submission_id AND s.unit_id=o.unit_id AND s.kind=o.kind AND s.group_id IS NOT DISTINCT FROM o.group_id AND s.planned_ad_id IS NOT DISTINCT FROM o.ad_id
), result_counts AS (
 SELECT submission_id,kind,outcome,count(*) n FROM results GROUP BY submission_id,kind,outcome
), outcomes AS (
 SELECT submission_id,jsonb_object_agg(kind || ':' || outcome,n) values FROM result_counts GROUP BY submission_id
)

