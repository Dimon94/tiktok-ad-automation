from string import Formatter
from uuid import UUID

from app.core.errors import DomainError

DEFAULT_NAME_TEMPLATE = "{provider_drama}-{drama_id}"
NAME_FIELDS = frozenset(
    {
        "provider_drama",
        "provider_label",
        "drama_name",
        "drama_id",
        "YYYYMMDD",
        "provider_pinyin",
    }
)
# 没有版权方剧目 ID 的手动剧目在命名时使用系统编号，前缀区分于版权方编号。
LOCAL_DRAMA_ID_PREFIX = "ada-"


def local_display_drama_id(drama_id: UUID) -> str:
    """按剧目记录生成固定的 ada-8位数字，同一剧目每次预览得到相同编号。"""
    return f"{LOCAL_DRAMA_ID_PREFIX}{drama_id.int % 10**8:08d}"


def validate_name_template(template: str) -> None:
    # 可使用组合字段，或拆成标识和剧名；所有字段均可自由排列。
    try:
        parts = tuple(Formatter().parse(template))
    except ValueError, TypeError:
        raise DomainError("invalid_name_template", "invalid_name_template") from None
    fields = [field for _, field, _, _ in parts if field is not None]
    if (
        len(template) > 1000
        or set(fields) - NAME_FIELDS
        or "drama_id" not in fields
        or not (
            (
                "provider_drama" in fields
                and not {"provider_label", "drama_name"}.intersection(fields)
            )
            or (
                "provider_drama" not in fields
                and {"provider_label", "drama_name"}.issubset(fields)
            )
        )
        or len(fields) != len(set(fields))
        or any(spec or conversion for _, _, spec, conversion in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in template)
    ):
        raise DomainError("invalid_name_template", "invalid_name_template")


def naming_parts(
    protected_base: str, title: str, provider_pinyin: str
) -> tuple[str, str]:
    """只拆分已知剧名后缀，归因标识内部文字/花括号完整保留，不猜测分隔段。"""
    if not protected_base:
        return provider_pinyin, title
    suffix = f"-{title}"
    if title and protected_base.endswith(suffix):
        return protected_base[: -len(suffix)], title
    # 版权方也可能直接提供不带剧名的归因名称，将完整原值作为标识。
    return protected_base, title


def render_names(
    *,
    protected_base: str,
    title: str,
    date_text: str,
    batch_short_id: str,
    group_no: int,
    creative_no: int,
    max_length: int,
    provider_pinyin: str,
    display_drama_id: str,
    template: str = DEFAULT_NAME_TEMPLATE,
) -> tuple[str, str, str]:
    validate_name_template(template)
    if any(
        type(value) is not int or value < 1
        for value in (group_no, creative_no, max_length)
    ):
        raise DomainError("invalid_group_config", "invalid_group_config")
    drama_id = display_drama_id.strip()
    digits = drama_id.removeprefix(LOCAL_DRAMA_ID_PREFIX)
    if (
        not digits.isascii()
        or not digits.isdigit()
        or (digits == drama_id and int(digits) < 1)
        or (not protected_base and (not provider_pinyin.strip() or not title.strip()))
    ):
        raise DomainError("naming_context_missing", "缺少有效版权方数字剧目 ID")
    provider_label, drama_name = naming_parts(protected_base, title, provider_pinyin)
    if "{provider_label}" in template and (
        not provider_label.strip() or not drama_name.strip()
    ):
        raise DomainError("naming_context_missing", "缺少版权方标识或剧名")
    # 网眼归因标识整体移动；独立拼音标签始终使用冻结的版权方拼音。
    # 插入值是字面文本，归因花括号不会被再次解析。批次编号由系统统一追加。
    campaign = (
        template.format_map(
            {
                "provider_drama": protected_base or f"{provider_pinyin}-{title}",
                "provider_label": provider_label,
                "drama_name": drama_name,
                "drama_id": drama_id,
                "provider_pinyin": provider_pinyin,
                "YYYYMMDD": date_text,
            }
        )
        + f"-{batch_short_id}"
    )
    group = f"{campaign}-g{group_no:02d}"
    ad = f"{group}-sp{creative_no}"
    if (
        not campaign.strip()
        or not batch_short_id.strip()
        or any(len(name) > max_length for name in (campaign, group, ad))
    ):
        raise DomainError("name_too_long", "name_too_long")
    return campaign, group, ad
