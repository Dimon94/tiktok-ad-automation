# M3 本地交付验收

范围：Linear JB-21（Epic JB-19 下的 M3），实施票 JB-22→JB-23，同一个 MMF。用户批准的验收入口为现有广告工作区→真实本地列表/详情 HTTP→真实 PostgreSQL；官方 SDK transport 使用受控替身。正式 [Spec](../superpowers/specs/2026-11-14-native-series-directory-spec.md) 已保留。

本地功能验收达成：授权账户可进入完全无报表的系列→广告组→广告→创意目录，按真实引用分页、回到父级并查看详情；原生详情默认展示 Series、独立组/创意身份、版位与精确 creative—平台素材关系。保留原配置、各层来源/观察时间/版本及未知或部分覆盖，未生成假报表行或放开管理动作。

## 版本与独立审查

- 基点：`55ee37146d7275b8c308161bce1986f9fbbb1670`。
- JB-22：实现 `2701fbfad95c316d8d5a49d8918fafdb2164edf4`，交接 `4ddb3e0031b53f1168b22a7185399285f0daa457`。
- JB-23：`d6e209c73f6af27637e7733185a5b92fb10bf1f4`。
- R1 P1 修复：`2937e6dc285b4f0be3ccc1e7c79ef8dc1f13d9c4`，详情投影处理合法 null 创意字段，保留未知和已观察事实。
- [Standards R1](2026-11-14-native-m3-review/review-r1-standards.md)：PASS，P0–P3 为0。
- [Spec R1](2026-11-14-native-m3-review/review-r1-spec.md)：BLOCK一个P1；[Spec R2](2026-11-14-native-m3-review/review-r2-spec.md)：PASS，P0/P1为0，该P1关闭。R2公开契约和依赖未改变，沿用Standards结论。

## 有效验证

[JB-22](2026-11-14-jb22-directory-navigation.md)：真实PG/HTTP/页面导航、范围分页、撤权和既有报表回归；root独立复跑3项通过。

[JB-23](2026-11-14-jb23-native-directory-recognition.md)：Testing1–10矩阵、真实SDK transport→原暂存/发布→PG→HTTP→页面主链、同名双Series、身份与素材错配探针、正式/demo构建；root在同源码独立复跑4项通过。

[P1修复](2026-11-14-native-m3-r1-null-fix.md)：两个合法null响应RED→GREEN，丢已观察创意/借组身份两个探针失败并恢复；受影响原生/详情9项通过（含页面）。Spec R2独立实跑8项通过，明确排除页面并继承既有页面证据。以上是多轮不同范围检查，不相加冒充唯一测试数。复用未变化的导航、权限、配置映射和构建结果；不重复全仓测试。

## 集成与边界

root接受此源码和证据，进入本地main快进集成；交付文档提交只增加本记录、正式Spec与审查报告，不改变已审产品源码。main的原有未跟踪research与前端生成文件保留。合入后的目标SHA、最小检查、Linear完成状态和任务树回收回执另保存在 `/tmp/tkada-native-series-20261112/m3-implementation/closeout.md`，不得以本段计划冒充已落地回执。

未push、PR、部署或真实TikTok/生产操作。真实平台返回字段、Series名称、目录完整覆盖及Celery/Beat宿主验收仍为Unknown。此次Done仅表示已批准M3本地实现和验收，不表示生产能力已验证；Epic JB-19仍保留后续MMF，不关闭。
