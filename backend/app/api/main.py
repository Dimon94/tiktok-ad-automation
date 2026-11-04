from fastapi import APIRouter

from app.api.routes import integrations, login, users, utils
from app.modules.accounts.capability_router import router as capability_router
from app.modules.accounts.mcp_router import callback_router as mcp_callback_router
from app.modules.accounts.mcp_router import router as mcp_accounts_router
from app.modules.accounts.router import router as accounts_router
from app.modules.ad_management.api import router as ad_management_router
from app.modules.automation_rules.api import router as automation_rules_router
from app.modules.builds.api import router as builds_router
from app.modules.builds.recovery_api import router as recovery_router
from app.modules.builds.submission_api import router as submission_router
from app.modules.builds.submission_catalog_api import (
    router as submission_catalog_router,
)
from app.modules.business.api import router as business_router
from app.modules.material_production.api import router as material_production_router
from app.modules.materials.ingest_api import router as ingest_router
from app.modules.materials.push_api import router as material_push_router
from app.modules.materials.push_api import source_router as material_source_router
from app.modules.materials.reissue_api import router as material_reissue_router
from app.modules.materials.router import router as materials_router
from app.modules.providers.router import router as providers_router
from app.modules.reporting.api import router as reporting_router
from app.modules.strategies.api import router as strategies_router
from app.modules.tenants.router import router as tenants_router

api_router = APIRouter()
api_router.include_router(login.router)
api_router.include_router(users.router)
api_router.include_router(utils.router)
api_router.include_router(integrations.router)
api_router.include_router(tenants_router)
api_router.include_router(material_production_router)
api_router.include_router(accounts_router)
api_router.include_router(ad_management_router)
api_router.include_router(mcp_accounts_router)
api_router.include_router(mcp_callback_router)
api_router.include_router(capability_router)
api_router.include_router(ingest_router)
api_router.include_router(materials_router)
api_router.include_router(material_push_router)
api_router.include_router(material_source_router)
api_router.include_router(material_reissue_router)
api_router.include_router(providers_router)
api_router.include_router(reporting_router)
api_router.include_router(business_router)
api_router.include_router(automation_rules_router)
api_router.include_router(strategies_router)
api_router.include_router(builds_router)
api_router.include_router(submission_router)
api_router.include_router(submission_catalog_router)
api_router.include_router(recovery_router)
