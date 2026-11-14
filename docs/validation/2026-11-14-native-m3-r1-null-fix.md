# M3 Spec R1：未知创意字段修复

起点 `d6e209c73f6af27637e7733185a5b92fb10bf1f4`。R1 Standards PASS；Spec BLOCK 的唯一 P1 是合法目录部分观察中的 `creative_list=null` 或独立子创意 `creative_info=null` 导致详情 500。原报告及基线复现在 `/tmp/tkada-native-series-20261112/m3-implementation/review-r1-spec.md`。

修复限现有详情投影 owner：身份投影接受未知配置并返回未知；内嵌创意清单未知时保留原始 null，记录 `creative_list:not_provided`，继续展示现有独立创意及素材观察。没有把 null 改成已知空清单，没有借用组身份，没有修改同步/发布、持久模型、公开 DTO 或前端。原 `creative_coverage:unknown` 保留。按 fix-root-causes 修复对合法未知值的错误假设；按 minimize-reader-load 没有新增层或平行 owner。

回归通过真实官方 SDK transport 合成响应→原暂存/发布→PostgreSQL→真实详情 HTTP。复用现有 fixture 并仅增加两个部分响应参数，不复制第二套发布器。两场景均先因原 TypeError RED，日志 `r1-null-red.log`：2 failed/5.12s；同一检查修复后 `r1-null-green.log`：2 passed/1.26s。检查断言原 Series/组身份与已观察两个 creative 均保留、原始 null 保留、未知身份/覆盖原因如实返回。

真实源码探针：把未知内嵌清单当空清单并丢弃已观察创意，断言失败；把未知创意身份回填为组身份，断言失败。脚本 `probe-r1-null.py`、两份 `r1-null-probe-*.log` 在上述仓外目录；finally 恢复并核对源码字节一致。

最终从任务树 backend 执行：

```bash
python3 /tmp/tkada-native-series-20261112/m3-implementation/run-test.py python -m pytest tests/modules/ads/test_native_directory.py tests/modules/reporting/test_detail.py -q --tb=short
```

9 passed/15.43s，包括原生 SDK→页面主 Seam、两个 null 分支及既有详情合同；日志 `r1-null-regression.log`。改动两 Python 文件 Ruff、diff check 通过。前端和生成契约没有变化，复用原 build；无其他测试扩量。原 `.venv/node_modules` 链接保留。

待独立 Spec R2 仅审此修复及影响行为；Standards R1 未阻断且公开 Interface/依赖未改变，沿用结论。M3 尚未放行/合入；未 push、PR、deploy、改 Linear、调用真实平台或生产。平台字段、Series 名称、完整覆盖及 Celery/Beat 宿主仍 Unknown。
