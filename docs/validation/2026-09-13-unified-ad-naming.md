# 2026-09-13 统一广告命名

> 2026-11-04 更新：本文中“版权方＋剧名必须在开头”的限制已取消，独立标签及自由排列见 [自由广告命名](2026-11-04-flexible-ad-naming.md)。以下保留历史验收记录。

用户已确认：仅保留一套名称格式，默认“版权方＋剧名－剧目 ID－批次编号”。网眼仅替换开头的版权方与剧名，后续字段与普通版权方一致。本文取代 2026-09-11 命名设计中的独立后缀配置和专用规则整名优先逻辑。

## 实现

- 策略编辑只保留“广告名称格式”，用 `[版权方＋剧名]-[剧目 ID]` 显示可编辑部分；提供中文字段按钮，可添加日期及固定文字、调整后续顺序与分隔符。批次编号作为固定的自动追加项展示，不需要用户填写变量。
- 版权方＋剧名必须在开头且仅出现一次，以保护归因前缀；剧目 ID 必填且取版权方外部 ID，不以网眼归因前缀中的编号代替。嘉书和网眼实时示例同步套用同一格式。
- API 仅保存 `campaign_name_template`，默认 `{provider_drama}-{drama_id}`，可选 `{YYYYMMDD}`；删除 `campaign_suffix`、旧变量及旧分支。编号由同一命名函数统一追加，广告组与广告继续追加 `-g01`、`-sp1`。
- 默认示例：嘉书 `jiashu-The Bond-106001-A7K2`；网眼 `{b30008/s328302/c3}-The Bond-328302-A7K2`。加入日期后，二者都在对应位置插入 `20260913`。示例剧目 ID 为演示值，运行时读取冻结的版权方外部 ID。

## 历史数据与发布边界

- 策略版本有不可变约束，不改写历史 JSON、请求摘要、冻结预览或已提交名称。存储读取边界 `read_saved_config` 将旧配置投影为当前格式，移除旧后缀、合并版权方与剧名字段，将随机号交给系统追加，并保留通用模板中的日期和固定文字。旧的独立后缀不再参与新命名，旧后缀里的日期不会自动加入通用模板。
- 这只是历史存储格式转换；所有新命名只经过同一个引擎，不保留旧版权方专用后缀渲染或回退逻辑。新请求携带已删除字段将被拒绝。
- 已冻结预览及提交继续使用已有名称快照；旧双模板或旧编号的 BUILDING 预览明确失败为 `preview_naming_outdated`，提示修改草稿后重新生成，防止断点恢复混用两套命名。
- 无表结构变更及数据库迁移。发布时 API 与 Worker 必须使用同一版本，按既有部署手册排空在执行任务；本轮仅本地实现、验证和提交，未推送、部署或调用真实广告接口。

## 验证

使用仓库已有本地专用测试环境运行；外部服务采用测试边界，不调用真实 TikTok。测试命令和结果见实施进度中的同名记录。

- 后端：统一命名、两版权方日期及固定文字同步、独立剧目 ID、必填字段/括号/重复字段/归因位置校验、批次编号碰撞、快照与历史配置保留、策略 API 保存/回查、预览生成。
- 浏览器：策略编辑、单配置保存后重开、中文字段及非法输入提示、网眼与嘉书三级名称同步；搭建预览与提交恢复页面回归。
- 1440px 与 390px 浏览器截图人工检查，命名区域及示例可读且无页面横向溢出。截图为本地演示数据，保存在 `/tmp/tk-ada-unified-naming-1440.png` 和 `/tmp/tk-ada-unified-naming-390.png`，不提交。
- 前端 TypeScript/Vite 构建、Biome、Python Ruff 与语法检查；生成 API 客户端。本轮仅提交生成类型中与命名有关的删除，保留并行任务的其他变更。

实际结果：后端 **102 passed**；浏览器联合回归 **64 passed**，最终策略回归 **38 passed**，最后解析调整定向复验 **2 passed**（重复用例不累加）。TypeScript/Vite 构建、Ruff、Biome、Python 语法和 `git diff --check` 通过。

```bash
# 专用 PostgreSQL 测试环境；仓库本地 runner 仅在进程内注入测试环境。
.superpowers/sdd/2026-09-11-tiktok-mcp-implementation/run-test -m pytest tests/modules/strategies tests/modules/builds/test_naming_numbers.py tests/modules/builds/test_naming_migration.py tests/modules/builds/test_previews.py -q --tb=short
# frontend/，PATH 包含仓库 .tools/node_modules/.bin
bunx playwright test tests/strategies.spec.ts tests/build-preview.spec.ts --project workspace --workers 2 --reporter line
bun run build
```
