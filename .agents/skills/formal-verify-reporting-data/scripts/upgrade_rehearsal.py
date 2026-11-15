"""Local isolated database, old writer process exits before additive upgrade."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

ROOT = Path.cwd()
old = "b0f3b1443e7fedc19f297d078a706d5f8ef50040"
from tests.database import require_test_database

require_test_database(os.environ["DATABASE_URL"])
url = make_url(os.environ["DATABASE_URL"])
if url.host not in {"localhost", "127.0.0.1", "::1"}:
    raise SystemExit("Local upgrade rehearsal requires a loopback test database")
name = "jb30_upgrade_test_" + uuid4().hex[:8]
admin = url.set(database="postgres", drivername="postgresql").render_as_string(
    hide_password=False
)
with psycopg.connect(admin, autocommit=True) as conn:
    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
env = os.environ.copy()
env["DATABASE_URL"] = url.set(database=name).render_as_string(hide_password=False)
with tempfile.TemporaryDirectory(prefix="jb30-upgrade-") as tmp:
    state = Path(tmp) / "seed.json"
    old_script = Path(tmp) / "old.py"
    old_script.write_text(
        '''import sys,importlib.abc,importlib.util,subprocess,json
from pathlib import Path
ROOT=Path.cwd(); commit="'''
        + old
        + """"
paths=set(subprocess.check_output(["git","ls-tree","-r","--name-only",commit],text=True).splitlines())
class GitCode(importlib.abc.MetaPathFinder,importlib.abc.Loader):
 def find_spec(self,fullname,path=None,target=None):
  if fullname!="app" and not fullname.startswith("app."): return None
  base="backend/"+fullname.replace(".","/")
  if base+".py" in paths: return importlib.util.spec_from_loader(fullname,self,origin=base+".py")
  if base+"/__init__.py" in paths: return importlib.util.spec_from_loader(fullname,self,origin=base+"/__init__.py",is_package=True)
  spec=importlib.util.spec_from_loader(fullname,loader=None,is_package=True);spec.submodule_search_locations=[str(ROOT/base)];return spec
 def create_module(self,spec): return None
 def exec_module(self,module):
  source=subprocess.check_output(["git","show",commit+":"+module.__spec__.origin],text=True)
  module.__file__=str(ROOT/module.__spec__.origin)
  exec(compile(source,module.__file__,"exec"),module.__dict__)
sys.meta_path.insert(0,GitCode())
from alembic.config import Config
from alembic import command
cfg=Config("backend/alembic.ini");cfg.set_main_option("script_location","backend/app/alembic")
command.upgrade(cfg,"actor_authorization_stamp")
from sqlmodel import Session
from app.core.db import engine,init_db
from tests.modules.reporting.sync_conftest import sync_seed,ad_row
from app.modules.reporting.publish import publish_ad_day,AccountMeta
from datetime import date
from uuid import uuid4
with Session(engine) as s:
 init_db(s);seed=sync_seed.__wrapped__(s)
 publish_ad_day(s,tenant_id=seed.tenant_id,job_id=uuid4(),accounts={"acc-1":AccountMeta("USD","UTC")},start=date(2026,11,13),end=date(2026,11,13),rows=[ad_row("legacy",date(2026,11,13),spend="10",total="20")])
 s.commit()
 Path(sys.argv[1]).write_text(json.dumps(dict(tenant=str(seed.tenant_id),actor=str(seed.actor_id))))
print("old writer committed 10/20 and exited")
"""
    )
    subprocess.run(
        [sys.executable, str(old_script), str(state)], cwd=ROOT, env=env, check=True
    )
    new_script = Path(tmp) / "new.py"
    new_script.write_text("""import json,sys
from uuid import UUID
from pathlib import Path
from types import SimpleNamespace
from alembic import command
from alembic.config import Config
cfg=Config("backend/alembic.ini");cfg.set_main_option("script_location","backend/app/alembic");command.upgrade(cfg,"head")
from app.core.db import engine
from sqlmodel import Session,select
from app.modules.reporting.store_models import AdReportDay,ParentReportDay
from app.modules.reporting.queries import query_ads
from app.modules.reporting.schemas import ReportingFilter
from datetime import date
from decimal import Decimal
ids=json.loads(Path(sys.argv[1]).read_text());tenant=UUID(ids['tenant'])
ctx=SimpleNamespace(tenant_id=tenant,actor_id=UUID(ids['actor']),role='operator')
with Session(engine) as s:
 ad=s.get(AdReportDay,(tenant,'acc-1',date(2026,11,13),'legacy'))
 assert ad.spend==Decimal(10) and ad.revenue_total==Decimal(20)
 assert not s.exec(select(ParentReportDay).where(ParentReportDay.tenant_id==tenant)).all()
 page=query_ads(s,context=ctx,bc_id='bc-a',filters=ReportingFilter(dimension='ad',start_date=date(2026,11,13),end_date=date(2026,11,13),advertiser_ids=('acc-1',)))
 assert page.items[0].metric_buckets[0].provenance['revenue_total'].contract_revision=='LEGACY_UNKNOWN'
 s.commit()
from app.alembic.versions import native_campaign_day,native_adgroup_day,native_frozen_query,native_frozen_export
for migration in (native_campaign_day,native_adgroup_day,native_frozen_query,native_frozen_export):
 try: migration.downgrade()
 except RuntimeError: pass
 else: raise AssertionError('unsafe automatic rollback')
print('upgraded: old amounts unchanged, LEGACY_UNKNOWN, no historical parent direct; rollback refuses')
""")
    subprocess.run(
        [sys.executable, str(new_script), str(state)], cwd=ROOT, env=env, check=True
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/modules/reporting/test_native_levels.py",
            "-k",
            "two_page_adgroup_worker_and_query and False",
            "-q",
            "-s",
            "--tb=short",
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )
print("independent upgrade rehearsal passed; database retained locally:", name)
