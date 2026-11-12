---
name: formal-verify-native-series-batch-submission
description: 修改原生多计划提交、冻结、执行围栏或完成判定时运行并维护有界 TLA+ 模型。
---

# 当前状态：草稿，不能作为 JB-16 形式验收通过

已实际运行 TLC 但尚未完成源码破坏探针、预算与三级逐创意的模型映射。
不能将本草模冒称代码全证明。R10/R11 由 JB-17/18 在同目录扩展。

## 运行

安装 Temurin Java 21 和 TLA+ v1.8.0 到任意本地目录（不改全局配置），设置
`TLA_JAVA` 为 Java 可执行文件绝对路径、`TLA_JAR` 为 jar 绝对路径：

```sh
.agents/skills/formal-verify-native-series-batch-submission/run.sh
```

本轮工具：Temurin 21.0.13+9、TLC 2026.11.06.222357 rev d8fb231（v1.8.0 release asset）。
初次抽象运行 220 generated / 100 distinct、深度 11，无反例。
扩展到两计划各四对象后：115300 generated / 16564 distinct、深度 35，无反例。
首次扩展由于 TLA+ 分支求值访问索引 0 出错，改用 IF 后通过；这是模型错误，不是业务反例。

## 覆盖与待核对映射

起点 820a26e9c2fef02b0d0136e8f0a93b8625ad3b1d；未设置已同步实现提交，当前仍为未验收工作树。

- Submit：`submissions.submit_preview` 的 draft/preview 行锁事务和 `uq_submission_preview`，同预览只有一个 Submission。
- Arm：`execution_state.arm_request` 的步骤行锁、write-once body/digest。函数名称已核对，仍须核对事务边界及重试分支。
- Send：`execution.process_step.before_request` 的实际发送围栏。模型把发送抽象为原子迁移；真实检查事务和网络之间的权限变更窗口仍需明确边界，不能隐去。
- Read：`native_completion.unit_native_matched` 的 MATCH。当前模型包含每计划四个有序对象，MATCH 抽象为 Read；未建模实际 creative 多重集合比较，不能用它证明该比较器。

两个 included 计划、一份 preview、一次授权失效、每计划四个抽象对象各一次发送、无恢复/取消。
现有不变量只覆盖模型自身的唯一提交、一次发送、冻结 body 及 MATCH 必须有创建。
金额仅按固定百分之一单位表达 10025 + 10025 = 20050，不能作为任意 Decimal R7 的证明；未覆盖权限/路由分别变化、多重集合、数据库与网络可靠性。
两个真实源码探针已运行：unit_native_matched 无条件返回 True、validate 强制首个 Unit，均被真实 PG 检查拒绝并恢复原文件。尚缺所有关键性质的源码及对应模型变体探针，不满足生成 skill 的最终验收。

## 完成此 skill 前必须做

按真实实现补齐 R5/R7/R8/R9/R12/R13 的模型和源码映射；真实源码破坏探针每项必须红，恢复字节后定向绿。模型对应变体也必须出现该性质的反例，不能仅搜索函数名。
若反例为真实缺陷，保留反例并补真实代码回归，不弱化性质。映射确认后再记录同步提交；后续修改用 maintain-formal-verification-skill 维护。
