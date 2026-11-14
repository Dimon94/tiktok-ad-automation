# M4b 素材实例效果本地验收

范围：JB33及五票JB34–38，基线1c987867971e8340ff793597b3c42c6acbb6fecc。用户指定fresh Codex6.1Sol/high，授权共享集成树串行唯一写者。没有push/PR/deploy/真实平台写入或生产迁移。

## 独立审查

完整固定head8d3629b9864389d0e8a6ff4b1b8ab53bd43d5c3e：Standards R1 PASS（P0/P1=0，P2=1）；Spec R1 PASS（P0/P1=0，P2=2）。报告保存在同目录native-m4b-review。两轴scope均已结束，未为非阻断小修额外开启R2。

## Root P2收尾

- JB41：真实PG保留旧素材投影后删除creative事实，生产查询先因eager默认参数isoformat(None)报错。改读publisher原观测，fallback只用投影原fetched_at，不依赖仍存活原事实或生成新时间。查询保留40、原观测时间与pending投影覆盖。
- JB42：手算父3−creative10=广告组残差−7，原CSV普通金额/币种/时区列为空。冻结及CSV保留−7/USD/Asia-Hong_Kong，仍标ADGROUP_RESIDUAL、NOT_COMPARABLE；无差额比率，不计普通素材10合计。残差单独详情可展示。
- JB43：原浏览器检查因缺导航按钮RED。复用当前详情权限/目录查询，仅控制导航可用性，不重写冻结金额；父Ad沿原日期范围linked详情，creative进入同tenant/BC/account目录详情。缺ID/来源不明/当前未同步/删除/无权限解释不可导航。浏览器验证父Ad及creative跳转、返回与缺目录creative原因。

实际检查：tracer+metrics首次14通过、1个新fixture主键元组错误；修正为四段实体主键后浏览器完整链1 passed/14.06s，替代该fixture失败。原有creative只有身份投影不等于当前目录对象；本地fixture增加真实目录行cr1验证可导航，cr2不补目录验证不可用解释。此前缺按钮RED与当前目录未同步轮失败均保留在会话证据，不改业务预期迁就。

两条新数据回归：2 passed；两项实际源码近似错误（伪造观测年份、残差取绝对值）均业务AssertionError RED→仓外备份finally恢复/cmp→2 GREEN。前端TypeScript+生产/演示构建通过，改动Python Ruff及diff check通过。格式化仅用仓外临时Biome配置处理目标TSX，未改仓库配置或依赖。

## 整个MMF与限制

五票逐AC、T1–T12/R1–R10、fresh/继承证据见JB34–38各验收记录。root先前独立阶段检查依次22+3、21+6、13、12、21通过；bounded模型对应32、12、32、24、64及源码拒错，不作无界或全系统证明。最高Seam贯通真实浏览器/HTTP/PGRedis/原同步与导出Worker/五类同快照CSV，仅SDK transport替身。P2后完整浏览器链再次通过。

新增本地迁移creative_fact_evidence为nullable evidence；历史金额保留，缺证据LEGACY_UNKNOWN，不回填历史。当前平台creative支持、D0窗口、自然付费组成、total成熟截止、远端S3故障行为和生产容量继续Unknown；不以SDKdouble替代平台证明。不声称全仓mypy全绿（既有诊断未重跑）。

结论：本地实现与验收PASS，可按原授权快进本地main并关闭本轮直接票；生产部署/迁移/真实平台验收另计。Epic JB19及其他阶段保持不动。
