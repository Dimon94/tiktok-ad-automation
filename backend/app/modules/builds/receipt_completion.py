"""创建回执已明确成功时，历史自动核查只保留审计事实，不再参与执行。"""

import re

from sqlalchemy import text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session

from app.modules.builds.execution_models import ExecutionStep


def confirmed_created_sql(alias: str = "s") -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", alias):
        raise ValueError("invalid SQL alias")
    # 与创建/回读契约的 ID 语法相同；只有落定成功且证据中无冲突的来源
    # 才能省略旧核查。UNKNOWN、仅有迟到回执、缺失 ID 都继续走原核实链。
    return f"""({alias}.status='SUCCEEDED' AND NOT {alias}.mismatch
 AND {alias}.remote_id ~ '^[A-Za-z0-9_.:-]{{1,255}}$'
 AND EXISTS (SELECT 1 FROM step_evidence receipt
  WHERE receipt.tenant_id={alias}.tenant_id
  AND receipt.submission_id={alias}.submission_id
  AND receipt.step_id={alias}.id
  AND receipt.conclusion IN ('CREATED','RECONCILED')
  AND receipt.summary->>'remote_id'={alias}.remote_id)
 AND NOT EXISTS (SELECT 1 FROM step_evidence conflict
  WHERE conflict.tenant_id={alias}.tenant_id
  AND conflict.submission_id={alias}.submission_id
  AND conflict.step_id={alias}.id
  AND conflict.conclusion IN ('CREATED','LATE_CREATED','RECONCILED')
  AND nullif(trim(conflict.summary->>'remote_id'),'') IS NOT NULL
  AND conflict.summary->>'remote_id'<>{alias}.remote_id))"""


def obsolete_readback_sql(alias: str = "s") -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", alias):
        raise ValueError("invalid SQL alias")
    return f"""({alias}.kind='READBACK' AND EXISTS (
 SELECT 1 FROM execution_step receipt_source
 WHERE receipt_source.tenant_id={alias}.tenant_id
 AND receipt_source.submission_id={alias}.submission_id
 AND receipt_source.unit_id={alias}.unit_id
 AND receipt_source.bc_id={alias}.bc_id
 AND receipt_source.id={alias}.parent_step_id
 AND receipt_source.kind IN ('CAMPAIGN','ADGROUP','AD')
 AND {confirmed_created_sql("receipt_source")}))"""


def is_obsolete_readback(session: Session, step: ExecutionStep) -> bool:
    if step.kind != "READBACK":
        return False
    return bool(
        SASession.execute(
            session,
            text(
                "SELECT "
                + obsolete_readback_sql("s")
                + " FROM execution_step s WHERE s.tenant_id=:tenant AND s.id=:step"
            ),
            {"tenant": step.tenant_id, "step": step.id},
        ).scalar_one()
    )
