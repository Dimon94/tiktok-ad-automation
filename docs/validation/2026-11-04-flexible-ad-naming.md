# 2026-11-04 自由广告命名

按用户要求，版权方标识和剧名不再绑定为固定顺序。

## 配置方式

在策略的“广告命名”中直接编辑标签位置、分隔符及固定文字：

- `[剧名]-[版权方标识]-[剧目 ID]-[日期]`
- `[日期]_[剧目 ID]_[版权方标识]_[剧名]`
- `[版权方标识]-[剧名]-[剧目 ID]`

旧的“版权方＋剧名”组合字段仍可用于完整归因名称，也可移动到任何位置；使用独立字段时删除组合字段，避免重复。独立字段必须同时包含版权方标识和剧名；剧目 ID 必填，日期和版权方拼音可选。每个字段只能出现一次。批次号及广告组、广告编号由系统自动追加。

网眼示例：`The Bond-{b30008/s328302/c3}-328302-20260913-A7K2`。标识从冻结归因名称中移除精确的 `-剧名` 后缀取得，不拆分标识内部内容；没有该后缀时完整使用原归因名称作为标识。普通版权方无归因名称时使用冻结版权方拼音。

## 识别与范围

本工具创建且名称仍与冻结快照一致的系列，使用实际远端搭建回执关联冻结剧目，而非靠名称字段位置推断；查询保持租户、账户和 BC 一致。平台另行改名或外部创建的系列继续按既有解析规则识别。没有更改经营看板的版权方/剧目身份规则。

无数据库结构变更。历史策略和已提交名称不改写。API 与搭建 Worker 发布时须同版本。仅本地提交，未部署、未推送、未调用真实广告接口。

## 验证

- 独立 PostgreSQL `naming_test`：`tests/modules/strategies/test_naming.py`、`test_saved_config.py`、`tests/modules/ads/test_directory.py` 及 `tests/modules/builds/test_previews.py -k free_naming`，共79通过。覆盖任意位置、网眼完整标识、预览冻结/提交展开、目录投影、跨租户/账户隔离及另行改名。
- 扩大到策略/预览/目录非 slow、非 MCP 回归：167通过，1失败。失败为未修改的 `test_grouping.py::test_structure_replay_is_stable_for_sorted_materials_and_copies`，合成素材缺少当前结构规划器要求的 `unit_id`；相关两个文件无本轮差异。
- 浏览器 `strategies.spec.ts --project workspace`：53通过，包含独立字段预览、保存、刷新回读、桌面/移动端布局。
- `tsc -p tsconfig.build.json`、正式与演示 Vite 构建通过。
- 变更 Python 的 Ruff 检查通过；`naming.py`、`naming_identity.py` 严格 mypy（follow-imports=silent）通过。
