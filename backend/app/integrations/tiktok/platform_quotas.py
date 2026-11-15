"""官方Marketing API额度：应用档位与物理接口，不能按业务动作拆分。

来源 https://business-api.tiktok.com/portal/docs?id=1740029171730433
核对日期2026-10-31；MCP服务额度须独立核实，不从Marketing API档位推断。
"""

LEVELS = {
    "BASIC": (10, 600, 864000),
    "ADVANCED": (20, 1200, 1728000),
    "PREMIUM": (30, 1800, 2592000),
    "ULTIMATE": (50, 3000, 4320000),
}

# 多种业务入口可能调用同一物理接口，必须复用相同额度桶。
SDK_ENDPOINTS = {
    "changelog.create": "POST /open_api/v1.3/changelog/task/create/",
    "changelog.check": "GET /open_api/v1.3/changelog/task/check/",
    "changelog.download": "GET /open_api/v1.3/changelog/task/download/",
    "/open_api/v1.3/oauth2/access_token/": "POST /open_api/v1.3/oauth2/access_token/",
    "music.get": "GET /open_api/v1.3/file/music/get/",
    "materials.get_videos": "GET /open_api/v1.3/file/video/ad/info/",
    "materials.search_videos": "GET /open_api/v1.3/file/video/ad/search/",
    "materials.get_images": "GET /open_api/v1.3/file/image/ad/info/",
    "materials.search_images": "GET /open_api/v1.3/file/image/ad/search/",
    "materials.get_suggested_covers": "GET /open_api/v1.3/file/video/suggestcover/",
    "materials.upload_video_url": "POST /open_api/v1.3/file/video/ad/upload/",
    "materials.upload_video_file": "POST /open_api/v1.3/file/video/ad/upload/",
    "materials.upload_image_url": "POST /open_api/v1.3/file/image/ad/upload/",
    "materials.share_assets": "POST /open_api/v1.3/creative/asset/share/",
    "scene.get_identity_video": "GET /open_api/v1.3/identity/video/info/",
    "scene.list_identities": "GET /open_api/v1.3/identity/get/",
    "scene.list_native_series": "GET /open_api/v1.3/identity/native_series/get/",
    "scene.list_minis": "GET /open_api/v1.3/minis/get/",
    "scene.recommend_ctas": "GET /open_api/v1.3/creative/cta/recommend/",
    "scene.check_vbo": "GET /open_api/v1.3/tool/vbo_status/",
    "scene.list_regions": "GET /open_api/v1.3/tool/region/",
    "finance.get_advertiser_balances": "GET /open_api/v1.3/advertiser/balance/get/",
    "build.list_optimizer_rules": "GET /open_api/v1.3/optimizer/rule/list/",
    "reports.integrated": "GET /open_api/v1.3/report/integrated/get/",
    "reports.bc": "GET /open_api/v1.3/report/integrated/get/",
    "reports.material_overview": "GET /open_api/v1.3/smart_plus/material_report/overview/",
    "reports.material_breakdown": "GET /open_api/v1.3/smart_plus/material_report/breakdown/",
    "ads.get_campaigns": "GET /open_api/v1.3/campaign/get/",
    "build.get_regular_adgroups": "GET /open_api/v1.3/adgroup/get/",
    "ads.get_ads": "GET /open_api/v1.3/ad/get/",
    "build.get_campaigns": "GET /open_api/v1.3/smart_plus/campaign/get/",
    "build.get_adgroups": "GET /open_api/v1.3/smart_plus/adgroup/get/",
    "scene.list_budget_adgroups": "GET /open_api/v1.3/smart_plus/adgroup/get/",
    "build.get_ads": "GET /open_api/v1.3/smart_plus/ad/get/",
    "build.create_campaign": "POST /open_api/v1.3/smart_plus/campaign/create/",
    "build.create_adgroup": "POST /open_api/v1.3/smart_plus/adgroup/create/",
    "build.create_ad": "POST /open_api/v1.3/smart_plus/ad/create/",
    "build.create_cta_portfolio": "POST /open_api/v1.3/creative/portfolio/create/",
    "build.get_cta_portfolio": "GET /open_api/v1.3/creative/portfolio/get/",
    "build.disable_adgroup": "POST /open_api/v1.3/smart_plus/adgroup/status/update/",
    "accounts.list_bcs": "GET /open_api/v1.3/bc/get/",
    "accounts.list_bc_assets": "GET /open_api/v1.3/bc/asset/get/",
    "accounts.list_bc_members": "GET /open_api/v1.3/bc/member/get/",
    "accounts.list_authorized_advertisers": "GET /open_api/v1.3/oauth2/advertiser/get/",
    "accounts.get_advertisers": "GET /open_api/v1.3/advertiser/info/",
}


def sdk_endpoint(
    operation: str, *, kind: str | None = None, smart: bool = False
) -> str:
    if operation in SDK_ENDPOINTS:
        return SDK_ENDPOINTS[operation]
    if operation.startswith("management."):
        if kind not in {"campaign", "adgroup", "ad"}:
            raise ValueError("management quota requires actual entity kind")
        root = "smart_plus/" if smart else ""
        action = operation.removeprefix("management.")
        if action == "set_material_status":
            if not smart or kind != "ad":
                raise ValueError("material switch requires Smart+ ad")
            suffix = "material_status/update"
        elif action == "set_status":
            suffix = "status/update"
        elif action in {"rename", "update_budget", "update_roas"}:
            suffix = "update"
        else:
            raise ValueError("unverified management endpoint")
        return f"POST /open_api/v1.3/{root}{kind}/{suffix}/"
    raise ValueError("SDK physical endpoint is not registered")


def sdk_quota(level: str, endpoint: str) -> tuple[int, int, int]:
    """特殊接口覆盖应用通用档位；返回QPS/QPM/QPD，不是并发数。"""
    if level not in LEVELS:
        raise ValueError("unverified TikTok application rate level")
    index = tuple(LEVELS).index(level)
    if endpoint == "POST /open_api/v1.3/ad/create/":
        return ((5, 150, 86400), (10, 200, 86400), (10, 300, 86400), (15, 300, 86400))[
            index
        ]
    if endpoint in {
        "POST /open_api/v1.3/creative/quick_optimization/create/",
        "POST /open_api/v1.3/creative/smart_video/create/",
        "POST /open_api/v1.3/video_template/task/create/",
    }:
        return ((1, 30, 5000), (2, 60, 10000), (3, 180, 15000), (3, 180, 15000))[index]
    return LEVELS[level]


def quota_configuration(level: str, *, max_inflight: int = 50) -> dict:
    """生成待部署配置；只输出非敏感额度参数，不修改环境文件或线上Secret。"""
    if (
        level not in LEVELS
        or type(max_inflight) is not int
        or not 1 <= max_inflight <= 50
    ):
        raise ValueError("invalid application level or resource concurrency")
    return {
        "sdk_level": level,
        "base": {
            "app_max_inflight": max_inflight,
            "tenant_max_inflight": max_inflight,
            "advertiser_max_inflight": max_inflight,
            "endpoint_max_inflight": max_inflight,
            # base仍为非SDK服务域的显式部署参数；不能套用SDK Basic到MCP。
            "app_calls_per_window": 10,
            "endpoint_calls_per_window": 3,
            "window_ms": 1000,
            "lease_ms": 960000,
        },
        "endpoints": {},
    }
