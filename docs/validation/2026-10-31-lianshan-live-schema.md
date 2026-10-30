# 连山 RDS 与参考系统生产 PG 只读核验（2026-10-31）

## 结论与证据边界

**生产 SSH 和消费侧 PostgreSQL 连接成功；连山源 RDS 连接失败，未取得本轮源端 `SHOW CREATE TABLE`、`SHOW INDEX` 或当前日表目录。** 2026-07-18 历史快照不能标记成 2026-10-31 实测。当前可确认的反差是：参考系统生产 PG 的四类报表使用普通表和索引，没有按日物理分表，也没有 PostgreSQL 声明式分区。

本轮遵循参考项目 `AGENTS.md`、`docs/production-ops/current-production-environment.md`。没有导入应用、运行迁移/测试/原巡检脚本、修改生产配置或查询业务明细，没有调用 TikTok MCP。凭据仅在进程内读取，未写入输出。原巡检脚本包含 `COUNT(*)` 和业务行抽样，本轮未执行。

## 连接记录（北京时间 UTC+8）

| 路径 | 精确时间 | 结果 |
| --- | --- | --- |
| 生产 SSH → 生产 `.env` 配置的连山 RDS | 2026-10-31 02:29:49.285418–02:29:55.317470 | PyMySQL `OperationalError 2003`，连接未建立；DNS 单独核验可解析 |
| 开发机 → 同一生产配置的连山 RDS | 2026-10-31 02:30:40.037218–02:30:45.063321 | PyMySQL `OperationalError 2013`，握手期间连接丢失；没有执行 SQL |
| 生产 SSH → 生产本机业务 PG | 2026-10-31 02:30:57.672127–02:30:57.716409 | 成功；数据库返回 `transaction_read_only=on` |

RDS 的主机、端口、用户名、密码来自生产配置，不从历史默认值猜取。开发机尝试时通过 SSH 在内存读取同一组 `OE_RDS_*`；没有使用本地 `DATABASE_URL`。另一次仅检查本地配置的尝试因缺少 RDS 键而停止，没有连接测试业务库。连接错误不能单独证明 RDS 停机，网络路径、访问控制或服务状态仍需另行排查。

连接限制：RDS connect 6 秒/read 12 秒；拟执行会话只读、单查询 8 秒限制。因连接未建立，这些源端会话限制也未落地。PG connect 6 秒、`default_transaction_read_only=on`、statement timeout 8 秒、lock timeout 1 秒；查询结束 rollback。PG 只读 `pg_class`、`pg_namespace`、`pg_inherits`、`pg_indexes` 和关系尺寸函数。

## 本轮生产 PG 事实

引擎：PostgreSQL **16.14**，Ubuntu 构建。服务器时间：2026-10-31 02:30:57.705406+08:00。

| 消费侧表 | 估计行数 `reltuples` | 总尺寸 bytes（包含索引/TOAST） | 类型 |
| --- | ---: | ---: | --- |
| `ad_report_data` | 394,177 | 1,254,850,560 | 普通表，无分区键 |
| `ad_material_hour_report_data` | 1,388,800 | 5,858,934,784 | 普通表，无分区键 |
| `uni_project_report_data` | 46,506 | 155,123,712 | 普通表，无分区键 |
| `uni_material_report_data` | 191,001 | 442,695,680 | 普通表，无分区键 |
| `report_ingestion_requests` | 4,255,827 | 11,970,764,800 | 普通表，无分区键 |
| `report_ingestion_failures` | 15,656 | 49,733,632 | 普通表，无分区键 |
| `rds_sync_cursors` | 360 | 180,224 | 普通表，无分区键 |

以上行数来自优化器统计，可能陈旧，**不是精确 COUNT**；尺寸是核验时实际关系文件尺寸。报表相关 `pg_inherits` 返回 0 行；四张事实表均 `relkind=r`、`pg_get_partkeydef=NULL`。本轮没有查业务时间最小/最大值，不声称 PG 当前数据完整或新鲜。

### 已核实的索引列顺序

所有下列索引均为 B-tree；括号内列顺序来自生产 `pg_indexes.indexdef`，不是代码预期。

| 表 | 索引 |
| --- | --- |
| `ad_report_data` | 唯一 `(advertiser_id, cdp_promotion_id, stat_time_hour)` **存在两份不同名称的相同列索引**；`(advertiser_id, stat_time_hour)`；`(stat_time_hour, advertiser_id, cdp_promotion_id, op_ts DESC, update_time DESC)`；`(advertiser_id, cdp_promotion_id, stat_time_hour DESC, op_ts DESC)`；`(op_ts)`；`(advertiser_id, cdp_project_id, stat_time_hour)`；`(stat_time_hour)` |
| `ad_material_hour_report_data` | 唯一 `(advertiser_id, cdp_promotion_id, material_id, image_mode, stat_time_hour)`；`(advertiser_id, stat_time_hour)`；`(material_id, stat_time_hour)`；`(op_ts)`；`(advertiser_id, cdp_project_id, stat_time_hour)`；`(stat_time_hour)` |
| `uni_project_report_data` | 主键 `(id)`；唯一 `(advertiser_id, project_id, stat_time)`；`(stat_time)` |
| `uni_material_report_data` | 主键 `(id)`；唯一 `(advertiser_id, material_id, image_mode, stat_time)`；`(stat_time)` |
| `rds_sync_cursors` | 主键 `(table_name)` |

两份竞价报表唯一索引名为 `ad_report_data_advertiser_id_cdp_promotion_id_stat_time_hou_key` 和 `ux_ad_report_data_advertiser_promotion_hour`。这里只报告观察，不在参考生产库执行索引清理。

## 源 RDS 历史快照（仅 2026-07-18 证据）

来源：参考仓库 `docs/superpowers/specs/2026-07-18-lianshan-rds-schema.md`，以及 `backend/app/integrations/lianshan_rds.py` 的客户端约定。

| 历史订阅类别 | 源表前缀 | 历史列数 | 历史索引列组（省略通用项） |
| --- | --- | ---: | --- |
| 升级版报表 | `lianshan_report_std_YYYYMMDD` | 174 | `(advertiser_id,cdp_promotion_id,stat_time,app_code)`；`(advertiser_id,cdp_project_id,stat_time)` |
| 升级版素材 | `lianshan_material_std_YYYYMMDD` | 143 | `(advertiser_id,material_id,stat_time,app_code)`；`(advertiser_id,cdp_project_id,stat_time)`；`(advertiser_id,cdp_promotion_id,stat_time)` |
| 全域项目 | `lianshan_report_uni_YYYYMMDD` | 80 | `(advertiser_id,project_id,stat_time)` |
| 全域素材 | `lianshan_material_uni_YYYYMMDD` | 53 | `(advertiser_id,image_mode,stat_time)`；`(advertiser_id,material_id,stat_time)` |

历史通用索引：主键 `(id)`、`(uid,stat_time)`、`(advertiser_id,stat_time)`、`idx_update_time(update_time)`。历史记录每订阅任务独立数据库、每库 20260718 和 20260719 两张日表，后者预建。历史抽样时两类素材表为 0 行；该事实不能延伸为今天仍无素材。

**当前未核实项**：源 MySQL 实际版本/存储引擎、当前可见数据库/表数量与日期范围、每日日表估计行数/bytes、日表内部是否另有原生 partition、当前字段/索引是否变化。历史文档只支持“按名称拆日表”，不能由此推断 InnoDB 或原生 partition 状态。不能把本轮 PG 的 138.9 万素材小时估计行数和历史源表的 0 行当作同表精确增量。

## 对 TK 全链路重构的启示

1. 分开源交付模型和消费侧查询模型。上游日表方便订阅/保留窗口；参考消费侧按业务维度及时间唯一键归并，页面不逐日扫描源表。TK 应围绕自己的租户、BC、账户、对象、日期与查询粒度设计事实键，而非照搬巨量表前缀。
2. 增量同步可借鉴 `(update_time,id)` 稳定排序、每张源表独立游标，以及增量窗口/历史对账分离。客户端代码体现这一合同；本轮没有证明源查询实际走索引或游标已追平。
3. 索引要由 TK 的查询条件与排序验证：等值租户/BC/账户范围，再时间范围与必要排序；业务唯一键应含完整隔离维度。参考 PG 没有 TK 租户/BC 列的索引，不适合直接复制。
4. 源物理日表不等于 TK 必须建日分区。本轮生产 PG 证明该参考系统尚用普通表；是否将 TK 按月分区，应结合保留周期、清理成本、实际量级和 `EXPLAIN` 决定，不能从上游拆表方式直接推出。
5. 保留摄入审计与错误隔离，但应有明确留存/压缩边界：参考 `report_ingestion_requests` 已约 11.97 GB，大于四张业务报表合计。此项仅为尺寸观察，没有执行 bloat 或查询性能诊断。
6. 不照搬重复索引、巨量指标单位、全域素材业务粒度或参考项目的 7/14 天窗口。TK 的跨时区日界、延迟归因、API 配额、租户和 BC 隔离仍需自身合同与验证。

## 私有证据与可复核性

本工作树 `.runtime/lianshan-live/` 已由 Git ignore 排除；原始 PG 元数据、失败连接记录和纯元数据巡检脚本均在其中，未提交。公开报告不包含连接端点、账号、密码、订阅库 ID、广告主 ID 或业务明细。

主要证据：`rds-schema.json`（生产路径失败）、`rds-schema-direct.json`（开发机路径失败）、`pg-schema.json`（实时 PG 元数据）。源 RDS 的 SQL 巡检方案已保存为 `audit_rds.py`，只有网络/握手恢复后才可获得缺失事实；届时仍应保持只读和超时，不使用原脚本的全表 COUNT/业务行打印。
