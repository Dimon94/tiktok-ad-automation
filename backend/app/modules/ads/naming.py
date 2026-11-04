"""历史外部系列的标准格式解析；本工具自由命名通过搭建回执读取冻结身份。"""

import unicodedata
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CampaignIdentity:
    provider_label: str | None
    drama_name: str | None
    status: Literal["VALID", "INVALID"]

    def __post_init__(self) -> None:
        if self.status not in ("VALID", "INVALID"):
            raise ValueError("invalid campaign identity status")
        for value in (self.provider_label, self.drama_name):
            if value is not None and (type(value) is not str or not value.strip()):
                raise ValueError("invalid campaign identity segment")
        if self.status == "VALID" and (
            self.provider_label is None or self.drama_name is None
        ):
            raise ValueError("valid campaign identity requires both segments")


def parse_campaign_name(name: str) -> CampaignIdentity:
    if type(name) is not str:
        raise ValueError("campaign name must be a string")
    # 此函数不反向猜测自由模板字段。保留历史格式空段，不能将备注前移充当剧名。
    parts = name.split("-", 2)
    if len(parts) < 2:
        return CampaignIdentity(None, None, "INVALID")
    provider, drama = (unicodedata.normalize("NFC", part.strip()) for part in parts[:2])
    if not provider or not drama:
        return CampaignIdentity(None, None, "INVALID")
    return CampaignIdentity(provider, drama, "VALID")
