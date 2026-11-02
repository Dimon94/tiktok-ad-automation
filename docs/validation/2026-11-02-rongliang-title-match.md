# 容量当前剧名与原名匹配修复

用户明确要求修复后发布生产。生产草稿的五部剧均返回 drama_not_found；只读核对容量真实接口，每个输入均返回唯一记录且 compilationsName 与输入完全一致，但 originalTitle 不同。旧代码优先取 originalTitle，误将合法的当前剧名匹配过滤掉。

搜索结果同时比较 compilationsName 和 originalTitle，任一完全匹配即可，候选标题保留实际匹配的输入名称。同名不同 ID 全部保留给准备流程消歧；不放宽大小写或子串匹配，不修改计划名别名规则、链接创建、素材选择或广告提交。

验证：新增回归在旧实现为 1 failed / 3 passed；修复后容量协议 30 项全部通过（HTTP 运输替身，无外部写入）。Ruff lint、两个脚本语法检查通过。strict mypy 的 3 处已有问题经同目录 shadow-file 基线确认完全一致，本轮无新增；容量相关两个文件原有格式不满足当前 formatter，未对无关代码做整文件格式化。

重新准备补充：旧草稿会按摘要复用 needs_resolution/drama_not_found，因此将此明确未匹配结果纳入重新准备的重查条件，继续保留同名消歧、ready 和 result_unknown 的原复用规则。新增五种状态回归在旧实现仅 drama_not_found 失败；修复后手动补链/准备与容量协议合计 56 项通过，使用独立真实 PostgreSQL 测试库及 Redis。drafts 模块 strict mypy 六处既有问题经 shadow-file 基线核对无新增，代码格式通过，既有测试文件有一处无关格式差异未扩散。

发布范围：容量 adapter 与重新准备的失败重查条件及测试/说明，无数据库结构、任务合同、依赖、运行配置、开关或并发变更。按现行快修流程执行针对性验证、精确版本和镜像核对、独占发布锁、Kubernetes 正常退出宽限及原验收；不创建备份、不执行恢复演练或额外全量排空。真实生产验收结果待发布后补记。
