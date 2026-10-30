"""将平台配置规范为窄字段；查询与迁移共享精确空值和真值解析语义。"""

from collections.abc import Sequence


def _json_num(expr: str) -> str:
    """JSON 值 -> numeric；非数字返回 NULL，与 dimension_query._number 一致。"""
    return (
        f"(CASE WHEN jsonb_typeof({expr}) = 'number' THEN ({expr} #>> '{{}}')::numeric "
        f"WHEN jsonb_typeof({expr}) = 'string' AND btrim({expr} #>> '{{}}') "
        r"~ '^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$' "
        f"THEN btrim({expr} #>> '{{}}')::numeric END)"
    )


def _json_truthy(expr: str) -> str:
    """Python 真值：缺失、null、false、0、空串/空数组/空对象都视为假。"""
    return (
        f"({expr} IS NOT NULL AND {expr} NOT IN ('null'::jsonb, 'false'::jsonb, "
        f"'\"\"'::jsonb, '[]'::jsonb, '{{}}'::jsonb) AND NOT (jsonb_typeof({expr}) = 'number' "
        f"AND ({expr} #>> '{{}}')::numeric = 0))"
    )


def _first_truthy_num(cfg: str, keys: Sequence[str]) -> str:
    """config.get(a) or config.get(b) ... 再转数字：取第一个真值，不再向后回退。"""
    branches = " ".join(
        f"WHEN {_json_truthy(f'({cfg} -> {key!r})')} THEN {_json_num(f'({cfg} -> {key!r})')}"
        for key in keys
    )
    return f"(CASE {branches} END)"


def _created_date(cfg: str, tz: str) -> str:
    """目录 create_time 按账户时区取本地日；无法解析返回 NULL。"""
    v = f"({cfg}->'create_time')"
    s = f"({v} #>> '{{}}')"
    epoch = (
        f"(CASE WHEN abs(({s})::float8) < 1e11 THEN "
        f"(to_timestamp(({s})::float8) AT TIME ZONE {tz})::date END)"
    )
    naive = (
        r"'^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?)?$'"
    )
    aware = (
        r"'^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?"
        r"(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'"
    )
    return (
        f"(CASE WHEN jsonb_typeof({v}) = 'number' THEN {epoch} "
        f"WHEN jsonb_typeof({v}) = 'string' AND {s} ~ '^[0-9]+$' THEN {epoch} "
        f"WHEN jsonb_typeof({v}) = 'string' AND {s} ~ {naive} "
        f"AND pg_input_is_valid({s}, 'timestamp') THEN ({s}::timestamp)::date "
        f"WHEN jsonb_typeof({v}) = 'string' AND {s} ~ {aware} "
        f"AND pg_input_is_valid({s}, 'timestamptz') "
        f"THEN ({s}::timestamptz AT TIME ZONE {tz})::date END)"
    )
