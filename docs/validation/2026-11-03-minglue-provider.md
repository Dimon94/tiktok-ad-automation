# 明略版权方接入：本地验证

## 交付范围

明略 `minglue` 已接入版权方连接、验证和应用发现、Token 恢复、精确剧名搜索、候选选择、起播集配置、持久取链及回读。前端使用现有连接表单和动态取链参数组件，未增加独立操作入口。

协议依据为同工作区的 `projects/minglue-drama-link-tool/minglue-link-cli.js` 与 README；开发期间未调用真实版权方或 TikTok 接口。模拟响应仅用于协议与恢复测试，不能当作真实接口验收证据。

## 行为与边界

- 凭据仅接受 `account/password`，沿用租户加密；不读 Node CLI 的账号文件或当前账号状态。
- 发现 `/App/List` 中 `packageType=4` 的 TT Minis 应用，保存实际 `partner_id/package_type/content_type`；业务请求显式携带所选 `partnerId/appId`。内部 appId 不冒充 TikTok Minis ID，目标 Mini 沿用现有搭建页的同名匹配及用户选择流程。
- 仅开放“起播集”，默认 1；逐页查剧集并按 `sortNo → sectionNo → 名称数字` 转成 sectionId。先核实当前应用可推广性，不调用付费修改或下载接口，不把剧单 feeSectionStart 当逐剧卡点。
- `/Promotion/Save` 按 CLI 的 TT Minis 字段规则提交，使用 tkMinisUrl，不以 H5/tinyUrl 替代。写入进入现有远端 scope 和 effect 台账；已有同参数记录必须详情回读通过才就绪。
- 名称含稳定任务标识，创建超时只恢复本任务名称对应的记录。列表为空不能证明未知写入不存在，保留只读恢复，不自动重放。分页缺项、错误应用/剧/剧集、缺失 Minis URL 都不能就绪。
- OCR 使用随锁文件安装的 ddddocr 1.6.1/ONNX，仅子进程内加载，通过 stdin 传图片，不保存验证码；15 秒单次上限、最多两轮验证码，登录整体预算 35 秒。业务请求沿用 30 秒超时，登录握手每次至多 5 秒；不使用终端交互或手工输入回退。识别失败有独立错误提示。

## 验证证据

本机 Python 3.14.6；全新专用 PostgreSQL `tkada_minglue_20261103_test`，测试通过 `APP_ENV_FILE=/dev/null` 隔离私有环境。HTTP 仅在传输边界模拟。

| 检查 | 结果 |
| --- | --- |
| `pytest backend/tests/modules/providers -q` | 392 项通过；随后增加的边界及最终实现见下列定向复验 |
| `pytest .../test_minglue_protocol.py .../test_minglue_workflow.py -q` | 最终 35 项通过（8.67 秒），包含登录预算、分页、选集、未知写入、应用/租户隔离和交错 scope 竞争 |
| `test_minglue_migration.py` | 升级、原连接保留、带数据降级拒绝、删除测试明略连接后正常降级/再升级通过；只在独立历史 schema 数据库执行 |
| Playwright `providers.spec.ts` 与 `build-link-config.spec.ts` | workspace 项目 28 项通过，包含明略账号字段与起播集提交 |
| `bun run --filter frontend build` | 正式及演示构建通过，OpenAPI 客户端由生成脚本更新 |
| 实际 OCR 子进程离线样例 | Pillow 生成 `AB12`，识别为 `AB12`，约 0.41 秒；不是明略真实验证码或账号联调 |
| Ruff、ty、Biome、语法与差异检查 | 全部通过；Biome 从 frontend 目录执行；新增 Python 文件格式检查通过 |

首次安装动态库时观察到 OCR 8 秒超时，实测冷 import 约 6.94 秒，模型初始化约 0.07 秒，已调整为上述有界预算并验证实际子进程；该本机样例不代表生产识别率或容器容量验收。

## 发布与真实联调待验

新增迁移 `0100_minglue_provider`，前序 `0099_transition_replan`，仅扩展 provider_connection 的 kind check 约束；有明略连接时拒绝降级，不删除连接或历史任务。未执行生产迁移、备份、推送或发布。

后续发布必须构建包含锁定依赖的新镜像（Linux 使用 opencv-python-headless），按部署手册安排 0100 迁移与新 Worker 步骤合同的同步切换；不新增功能开关，不依赖开发机 OCR 虚拟环境。当前仅完成 Python 3.14 本机安装与离线烟测，未构建/运行 Linux 候选镜像。

真实验收仍需目标租户中配置明略连接并验证应用发现、同名 Mini 匹配与一次授权剧目的取链/回读；验证码识别率、详情字段和真实端到端结果据该轮实际响应记录。未配置真实连接，也未创建真实推广链接或广告。

提交：本记录随 `providers: 接入明略版权方投放流程` 同次提交。未推送、未发布。
