-- Frozen regression oracle from 217986b; small test fixtures only.
WITH scope_basis AS (SELECT u.*, EXISTS (
 SELECT 1 FROM build_submission old JOIN build_unit old_u
 ON old_u.tenant_id=old.tenant_id AND old_u.preview_id=old.preview_id
 WHERE old.tenant_id=:tenant AND old.draft_id=:draft AND old.ordinal<:ordinal
 AND old_u.drama_id=u.drama_id AND old_u.advertiser_id=u.advertiser_id
 AND old_u.complete AND old_u.readiness IN ('READY','PREPARING')) covered
    FROM build_unit u WHERE u.tenant_id=:tenant AND u.preview_id=:preview),
    scope AS (SELECT b.*, (b.complete AND b.readiness IN ('READY','PREPARING') AND NOT b.covered) included FROM scope_basis b)
    , material_groups AS (
SELECT g.id group_id,g.unit_id,
 count(m.material_id)>0 AND coalesce(bool_and(ms.status='SUCCEEDED'),false) AND count(m.material_id)=count(ms.id) ready,
 coalesce(bool_or(ms.status='FAILED'),false) failed
FROM planned_group g
LEFT JOIN frozen_group_material m ON m.tenant_id=g.tenant_id AND m.preview_id=g.preview_id AND m.drama_id=g.drama_id AND m.group_no=g.group_no
LEFT JOIN execution_step ms ON ms.tenant_id=g.tenant_id AND ms.submission_id=:submission AND ms.unit_id=g.unit_id AND ms.kind='MATERIAL' AND ms.material_id=m.material_id
WHERE g.tenant_id=:tenant AND g.preview_id=:preview AND (CAST(:unit AS uuid) IS NULL OR g.unit_id=CAST(:unit AS uuid))
GROUP BY g.id,g.unit_id
),
 objects AS (
 SELECT u.id unit_id,'CAMPAIGN' kind,NULL::uuid group_id,NULL::uuid ad_id FROM scope u WHERE included
 UNION ALL SELECT u.id,'ADGROUP',g.id,NULL FROM scope u JOIN planned_group g ON g.tenant_id=u.tenant_id AND g.preview_id=u.preview_id AND g.unit_id=u.id WHERE included
 UNION ALL SELECT u.id,'AD',g.id,a.id FROM scope u JOIN planned_group g ON g.tenant_id=u.tenant_id AND g.preview_id=u.preview_id AND g.unit_id=u.id JOIN planned_ad a ON a.tenant_id=g.tenant_id AND a.preview_id=g.preview_id AND a.group_id=g.id WHERE included),
 results AS (
 SELECT o.kind, CASE
 WHEN nullif(trim(s.remote_id),'') IS NOT NULL THEN 'succeeded'
 WHEN s.status IN ('SUCCEEDED','UNKNOWN') THEN 'unknown'
 WHEN s.status='FAILED' OR EXISTS (SELECT 1 FROM execution_step ancestor WHERE ancestor.tenant_id=:tenant AND ancestor.submission_id=:submission AND ancestor.unit_id=o.unit_id AND ancestor.status='FAILED' AND (ancestor.kind='CTA' OR (ancestor.kind='CAMPAIGN' AND o.kind!='CAMPAIGN') OR (ancestor.kind='ADGROUP' AND o.kind='AD' AND ancestor.group_id=o.group_id)))
 OR (o.kind='CAMPAIGN' AND EXISTS (SELECT 1 FROM material_groups mg WHERE mg.unit_id=o.unit_id) AND NOT EXISTS (SELECT 1 FROM material_groups mg WHERE mg.unit_id=o.unit_id AND NOT mg.failed))
 OR (o.kind IN ('ADGROUP','AD') AND EXISTS (SELECT 1 FROM material_groups mg WHERE mg.group_id=o.group_id AND mg.failed)) THEN 'failed'
 ELSE 'pending' END outcome
 FROM objects o LEFT JOIN execution_step s ON s.tenant_id=:tenant AND s.submission_id=:submission AND s.unit_id=o.unit_id AND s.kind=o.kind AND s.group_id IS NOT DISTINCT FROM o.group_id AND s.planned_ad_id IS NOT DISTINCT FROM o.ad_id)
 SELECT kind,outcome,count(*) n FROM results GROUP BY kind,outcome
