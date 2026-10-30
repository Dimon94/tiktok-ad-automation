# 同步与查询隔离基线（2026-10-31）

状态：W0 小样本/扩容阶段，**未完成 spec §8 全量混合验收**。没有生产读写，没有启动应用服务、Celery 或任何远端 TikTok 调用。

## 可复现边界

- 专用本机 PostgreSQL `sync_redesign_test_perf`，仅允许 localhost/127.0.0.1/::1、5432 和精确库名；拒绝 URL 查询参数覆盖 host、其他 test 库或业务库。`APP_ENV_FILE=/dev/null` 必须显式设置；不加载项目真实 `.env`。
- `.runtime/run.py perf` 提供独立环境；Redis 分配为 59379/db4，本轮查询/发布基线未访问 Redis。依赖仅使用工作树 `.venv`。
- 旧版保存完整 `4656997e:backend/app` 到忽略目录 `.runtime/sync-query/old-4656997e/`。`--code old` 在首次导入 `app` 前切换整个源码路径，避免只替换一个函数而混用新版依赖；使用同一隔离 PG、同一 fixture manifest。该快照不含环境文件。
- 脚本不删除数据库、不清表、不清容器、不清系统缓存；已存在 manifest 拒绝覆盖。测试/失败构造留下的小范围专用测试数据保留。因此各阶段 PG 目录实际包含此前阶段的独立租户，原始结果记录真实存储增量和环境。
- 生成样本和计时原文均为合成数据，保存在 `.runtime/sync-query/`（Git ignored）；不提交原始运行结果。

## 固定数据合同

生成器 v2：seed=413，结束日期 2026-10-30，90 个当地日，5 个合成账户，USD，UTC/Asia/Hong_Kong 两种时区。账户内严格建立 campaign→adgroup→ad 父关系；10% 广告有事实，其他目录只用于 include_empty/无关历史成本。每广告1个VIDEO引用与帖子链接。广告目录 JSON 约2KiB，用逐行 MD5 序列构造，避免一段重复字符制造不真实的高压缩率。

金额通过整数“分”公式生成，`Decimal` oracle 独立于生产聚合实现。所有维度查询都限制在 manifest 的账户列表内。首轮仅单币种、稳定VIDEO类型，没有完整模拟轮播、类型纠正、跨币种或晚到修订；这些仍属后续正确性与混合矩阵工作。

| 阶段 | ad 数 | 目录总行 | 引用行 | 广告日事实 | 素材日事实 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 小样本 v2 | 1,000 | 1,300 | 1,000 | 9,000 | 9,000 |
| 初始基线 v2 | 10,000 | 13,000 | 10,000 | 90,000 | 90,000 |
| 扩容 v2（待下方结果补注） | 100,000 | 130,000 | 100,000 | 900,000 | 900,000 |

另有 campaign 日汇总、命名与帖子关联表；这些同样计入空间测量。本轮“100k”指广告数，**不是**2693万目录/3504万引用生产等效规模。

## 空间预算

每次先测真实关系 heap、普通索引、TOAST（含TOAST索引），用 `pg_current_wal_insert_lsn` 差量记录 WAL。WAL 是同一 PG 集群的时间区间量，其他隔离库并发测试会计入，不能宣称精确由此夹具独占产生。

| 样本 v2 | heap bytes | index bytes | TOAST bytes | WAL bytes |
| --- | ---: | ---: | ---: | ---: |
| 1k 广告 | 4,259,840 | 6,758,400 | 3,604,480 | 21,746,536 |
| 10k 广告 | 43,491,328 | 54,059,008 | 36,093,952 | 204,992,720 |

生成门禁：按样本最大目录/活跃事实增长比例外推，额外保留一份索引构建空间，对“关系+WAL+索引构建”乘1.5，再强制留下至少10GiB可用磁盘。最初样本时本机剩余约31GiB；100k广告预测可容纳。对大于样本的规模没有实测前不会跳过门禁。

完整混合规模在该机器上不可直接执行：仅约2693万条2KiB目录JSON原文就约51.4GiB，已经超过可用空间，尚未计入引用、事实、索引、TOAST和WAL。初始保守外推的同分布2693万“广告”样本峰值约1.68TiB，**这是含90日稀疏双事实、35百万目录的夹具扩展模型，并非生产库容量预测**。它只说明不能盲目在31GiB空闲机器上生成。

可行后续路径：保留1k/10k/100k可重建阶段，分别测页内无关历史放大与索引/发布成本；将完整2693万目录+3504万引用的同一混合规模迁到有实测容量余量的隔离 PG 主机/磁盘，先按实际列宽重新采样估算。阶段测量不替代完整规模、20会话、同步混合或24小时门槛；本轮没有下调SLO。

## 旧版实测（4656997e）

1k与10k两个 v2 样本均完成六维度×1/7/31/90日的24个新快照查询，每个查询后再读已有快照页。所有查询的汇总消耗均与独立 Decimal oracle 相同；原始 JSON 保存汇总、前50个成员、结果摘要、SQL条数/耗时、进程峰值RSS。10k结果如下，单位毫秒：

| 维度 | 1日首屏/页 | 7日首屏/页 | 31日首屏/页 | 90日首屏/页 |
| --- | ---: | ---: | ---: | ---: |
| account | 224.6 / 13.6 | 26.8 / 12.2 | 27.8 / 13.3 | 39.6 / 19.2 |
| campaign | 237.4 / 99.7 | 27.1 / 13.9 | 26.4 / 12.0 | 40.9 / 16.6 |
| adgroup | 92.4 / 55.1 | 39.5 / 17.0 | 89.3 / 25.9 | 279.0 / 43.5 |
| ad | 65.4 / 22.1 | 59.1 / 15.2 | 89.8 / 18.6 | 242.6 / 32.1 |
| material | 3123.7 / 16.3 | 2559.7 / 12.8 | 578.4 / 22.5 | 734.1 / 35.9 |
| drama | 47.7 / 13.6 | 29.4 / 12.2 | 33.5 / 13.1 | 43.5 / 16.5 |

这是**每格一次的阶段测量，不能叫P95/P99**。新查询快照不等于冷PG/OS缓存；先后顺序、prepared/generic计划与缓存均可能影响结果。素材1/7日比31/90日慢的现象不能据此归因为日期越短算法越慢。脚本为最慢的三种候选SELECT保存真实 `EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON)`，原文在对应 old 运行结果内。

真实旧 publisher 独立测试：在基线日期范围外的2026-10-31发布单账户1000广告，`publish_ad_day`＋commit共0.1769秒；落库消耗45,525.00与oracle一致，进程峰值RSS约130MB。此项未预热/重复统计，不能当作发布P95或最大桶≤10秒证明。直接bulk seed的时间不标为生产publisher性能。

## 已验证和待验证

安全/确定性测试先RED（模块缺失），实现后11项通过，包含专用库拒绝、固定分金额、容量余量及真实PG父身份/金额检验。Ruff检查/格式化覆盖三个本轮脚本。

待完成：100k旧基线与新模型同数据回填对照；真正冷/热缓存分组；search/include_empty/不同排序；1/5/20会话及20不同冷查询与20相同单飞；固定50行且无关历史10倍；归因/类型/币种完整oracle；最大桶与混合同步；λ/μ、pool wait、调度历史10倍、3×15分钟/24小时。因此**W0尚未完成，整体方案不能结案**。

## 运行命令

从该工作树根目录执行，输出路径相对 runner 切换后的 backend：

```bash
python3 .runtime/run.py perf -m pytest tests/performance/test_sync_query_baseline.py -q
python3 .runtime/run.py perf ../scripts/sync-query-benchmark.py seed --objects 1000 --manifest ../.runtime/sync-query/sample-1000.json --output ../.runtime/sync-query/sample-1000-run.json
python3 .runtime/run.py perf ../scripts/sync-query-benchmark.py query --code old --manifest ../.runtime/sync-query/fixture-v2-10000.json --output ../.runtime/sync-query/old-v2-10000.json --explain
python3 .runtime/run.py perf ../scripts/sync-query-benchmark.py publish --code old --objects 1000 --manifest ../.runtime/sync-query/fixture-v2-10000.json --output ../.runtime/sync-query/old-publish-1000.json
```

已有manifest不重复生成；直接使用其固定身份重跑新旧查询。新代码导入前必须完成专用库迁移，并将同一manifest的目录/命名/引用与事实桶经正式rebuild入口回填；未回填导致空结果不算性能提升。

## 100k阶段补注

100k广告（130k目录、100k引用、两类各900k日事实）生成完成，59.51秒，进程峰值RSS127,811,584 bytes；关系增量heap443,383,808、index565,149,696、TOAST360,906,752 bytes，WAL区间2,095,025,688 bytes。生成后仍有29.32GiB空闲，没有触碰10GiB余量门禁。

环境实测为本机10 CPU、16GiB内存、PostgreSQL 17.5（Homebrew/aarch64）、shared_buffers=128MB、work_mem=4MB、max_connections=100；schema为0071_query_read_models。旧代码读取同一新schema的既有源表，新增读模型并未替代旧查询；这不是生产硬件。

旧版100k首轮在 material/1日触发其原有 `snapshot_transaction` 的30秒statement_timeout。未提高timeout；整体运行32.10秒，结果complete=false。之前成功的account/campaign/adgroup/ad首屏约244/70/145/1310ms。错误发生在物化成员/汇总SQL，保留 `.runtime/sync-query/old-v2-100000.json`，不把超时排除后宣称达到SLO。

runner补充 `--continue-on-error`：分别保存失败case的SQLSTATE与耗时，继续其他case；只要一项失败，结果仍为complete=false。`rebuild` action通过正式有界回填与dirty projector入口建立新读模型，每页提交，不手工伪造新模型事实。

按100k阶段前后实际关系尺寸进一步拆分估算（前后包含两次30广告安全测试的小幅增量，故每行量为近似值）：`ad_object`约3,131 bytes/目录身份，`ad_material_reference`约659 bytes/引用。按目标2693万目录与3504万引用分别外推，这两类关系约78.5GiB＋21.5GiB；尚未计入帖子链接、报表事实、派生模型、流水、额外索引构建和WAL。这个以真实存储而非原始JSON宽度的估算也已超过本机空间。因此分阶段测量可继续，完整混合规模必须换容量充足的隔离环境，并重新测峰值预算。

100k非素材20case补测全部完成且金额oracle通过，原文 `old-v2-100000-rest.json`。广告1/7/31/90日首屏约1.73/2.38/2.23/2.88秒，已有页约22/44/70/64ms；广告组90日首屏约1.89秒；账户90日已有页约246ms。仍是单次结果，无生产等效/P95声明。

素材100k单独续跑四窗口全部完成、金额oracle一致：1/7/31/90日首屏21.67/19.88/2.10/7.44秒，已有50行页103/44/103/167ms，RSS峰值120MB，原文 `old-v2-100000-material.json`。这是首轮30秒失败后已有缓存条件下的再次运行，**不撤销首轮超时证据**；也说明相同数据的计划/缓存/同时负载会明显影响结果，后续必须固定采样顺序与冷热口径、重复并收集计划，不能挑最小值。至此100k阶段24case均已有成功观察，另保留material/1日失败观察。
