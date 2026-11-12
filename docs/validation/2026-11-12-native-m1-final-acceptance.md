# 端原生 Series M1 本地交付验收

本地开发交付完成；Spec JB-5 与 JB-7～JB-12 六票均已读回 Done。没有推送、部署、真实广告、授权或版权方业务操作。

## 源码、评审与集成

- 集成起点：`da190b59d66bc08b1e67890e31a250af50a5a7d2`，保留原业务报表/历史日志的本地提交。
- M1 最后产品修复：`d39f6bbe7f8886fccb942c4de9abc02345f4e9ec`。R1 Standards 为2项P1/1项P2，Spec为3项P1/1项P2；发现的问题全部修复，没有把实现者自评当作放行。
- R2 新上下文、两轴独立复审均 PASS；未解决 P0/P1/P2/P3 均为0。R2只审修复及影响行为，其他沿用R1，未开启第三轮。
- 原生实现之外，已重新获取并集成远端 `74ea9bc0369a8157ec8a30131c9e200bea8dbe6d`（4项报表创意身份补读提交）。SSH获取因本机公钥失败，HTTPS获取成功；origin保持原SSH URL。只有实施进度文档发生合并冲突，保留两边完整记录，没有改写既有历史。
- 集成源码提交：`1134520cc5254ee3791008d4704194878fd0a7dc`；已快进到本地main。核对M1 builds/accounts/integrations/frontend/migration源码与R2已审tip无差异，远端4项更新限定reporting和进度文档。
- 本验收及完整R1/R2报告随附文档提交；该文档提交快进main后不改变已审/已验的产品源码。

完整两轴报告：

- [Standards R1](2026-11-12-native-m1-review/r1-standards.md)
- [Spec R1](2026-11-12-native-m1-review/r1-spec.md)
- [Standards R2](2026-11-12-native-m1-review/r2-standards.md)
- [Spec R2](2026-11-12-native-m1-review/r2-spec.md)
- [四类修复与逐项证据](2026-11-12-native-m1-review-r1-fixes.md)

## 可使用的链路

已有Series/双角色身份/地区选择 → 目标账户逐条视频及封面准备 → 沿原结构算法冻结准确预览 → 具体预览确认提交 → CTA/三级对象直接ENABLE创建 → 三级及逐创意全MATCH后完成 → UNKNOWN只读恢复/取消未发送本地步骤。

保留原固定/按素材、SHARED/SEQUENTIAL_AVERAGE及基础广告×creative_count，未采用新5+1分片或视频×文案笛卡尔积。没有业务剧目级全局身份限制；相同身份也必须有当前账户/资产/权限证据，不同身份组合缺证据仍UNVERIFIED。默认/独立封面和重复视频位置都按冻结选择发送；缺一条不缩组。取消不远端停用广告，在途/迟到回执与UNKNOWN保留。

## 协调者独立验收与复用证据

所有Python检查经 `/tmp/tkada-native-series-20261112/implementation/run-test.py`，固定隔离venv、专用本机真实PostgreSQL/Redis、当前源码PYTHONPATH；私有环境值不输出。平台唯一替身为官方SDK传输，浏览器导航/认证fixture边界见修复记录。

| 检查 | 实际结果 |
| --- | --- |
| JB-7真实草稿/SceneJob接续，停在素材未准备 | 1 passed / 3.65s |
| JB-8全素材同序就绪及缺一视频整组拒绝 | 2 passed / 5.84s |
| JB-9完整准备→FROZEN→明细回读→重复生成复用 | 1 passed / 4.73s |
| JB-10并发提交→1基础×6文案的SDK ENABLE wire/ID/精确预算→NEEDS_REVIEW | 1 passed / 13.80s |
| JB-11全MATCH与视频差异的完整创建/回读/结果时间线 | 2 passed / 30.30s |
| JB-12 UNKNOWN唯一恢复及在途取消保留真实ID | 2 passed / 15.03s |
| R1修复后真实浏览器点击提交/丢响应找回/返回任务→真实后端唯一Submission→SDK创建与回读COMPLETED | 1 passed / 47 deselected / 23.77s；仅uvicorn/websockets弃用警告 |
| 远端更新合并后，reporting目录补读与原生readback专项 | 158 passed / 8.15s |
| 本地main快进后原生readback专项 | 94 passed / 0.16s |

最后三项实际命令（前两项从集成树backend，最后从main的backend运行）：

```sh
python3 /tmp/tkada-native-series-20261112/implementation/run-test.py python -m pytest tests/modules/builds/test_native_execution.py -k browser_submission -q --tb=short
python3 /tmp/tkada-native-series-20261112/implementation/run-test.py python -m pytest tests/modules/reporting/test_directory_refresh.py tests/modules/builds/test_native_readback.py -q --tb=short
python3 /tmp/tkada-native-series-20261112/implementation/run-test.py python -m pytest tests/modules/builds/test_native_readback.py -q --tb=short
```

复用匹配源码的实现者证据：修复后相关回归185 passed；最终UI/Scene与能力TTL三项3 passed；TypeScript/正式与demo构建及静态检查通过。六票的PG/Redis、锁/幂等/UNKNOWN/取消、字段消融与错误探针结果见各票记录。不将重叠测试计数相加当作唯一用例总数，不重复无变化的176/173等全套。

## Tracker与交付范围

Junbo/JB为公司团队，项目ads-decision-system。2026-11-12T15:47Z逐票写回源码、验收和限制后读回：JB-7、JB-8、JB-9、JB-10、JB-11、JB-12均Done；再核对父下只有这六票且全Done，JB-5也读回Done。没有归档或更动其他任务。

主工作树原有未跟踪docs/research、tsbuildinfo与Vite生成文件保留；不stash/reset/clean。仅回收已合入的本次M1树；其他任务树保持原位。

## 尚未覆盖的外部范围

- 没有真实平台广告接受、OAuth、审核/经营资格或经营收益验收；真实写验收需具体冻结预览的独立授权，生产发布另行授权。
- active完整状态映射、跨账户/BC占用及任意不同身份组合支持仍按Spec保留Unknown，不宣称全域无冲突。
- 既有Mini身份目录fixture、旧预览SQL/签名和总数上限断言、旧Mini UNKNOWN及三项旧UI断言的动态基线失败保留在各票/修复记录。没有声称全仓全绿。
- 有限时间线与属性检查不是完整并发或形式证明；没有运行形式验证器。
