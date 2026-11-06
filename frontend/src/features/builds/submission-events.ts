// 日志先说明业务动作，原始事件码由展开的技术记录保留。
const conclusions: Record<string, string> = {
  REQUEST_ARMED: "已进入请求发送阶段",
  CREATED: "已记录创建成功",
  REUSED: "已复用原任务的成功对象",
  LATE_CREATED: "已收到延迟创建结果",
  RESULT_UNKNOWN: "创建结果待核实",
  UNKNOWN: "结果待核实",
  MATERIAL_UNKNOWN: "素材处理结果待核实",
  READBACK_PAGE: "已读取一页核查结果",
  RECONCILED: "已完成本次结果核查",
  RECONCILIATION_REQUESTED: "已申请核查结果",
  MATERIAL_RECONCILIATION_REQUESTED: "已申请核查素材",
  COVER_RECONCILIATION_REQUESTED: "已申请核查封面",
  RETRY_REQUESTED: "已申请重试",
  DEPENDENCY_RECOVERED: "前置步骤已恢复",
  DEPENDENCY_FAILED: "前置步骤失败",
  READBACK_BLOCKED: "结果核查受阻",
  READBACK_ERROR: "结果核查未完成",
  LATE_READBACK: "已收到延迟核查结果",
  LEASE_EXPIRED_ARMED: "请求等待超时，需核实结果",
  NOT_SENT: "请求未发送",
  LOCAL_FAILED: "本地检查失败",
  LOCAL_RETRYABLE: "本地处理可重试",
  LOCAL_SUCCEEDED: "本地处理完成",
  SUCCEEDED: "处理成功",
  FAILED: "处理失败",
  VERIFIED_REPLACEMENT: "补建结果已核实",
}

export function eventConclusion(code: string) {
  return conclusions[code] || "已记录执行事件"
}

export function eventKind(kind: string) {
  return (
    (
      {
        CAMPAIGN: "广告系列",
        ADGROUP: "广告组",
        AD: "广告",
        MATERIAL: "素材准备",
        CTA: "行动按钮准备",
        READBACK: "结果核查",
      } as Record<string, string>
    )[kind] || "任务步骤"
  )
}
