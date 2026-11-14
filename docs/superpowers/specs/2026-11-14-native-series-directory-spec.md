# M3 原生 Series 广告目录识别 Spec

状态：已定稿，root 已复核；用户已确认主验收 Seam。按当前对话授权发布为 JB-19 的 M3 Spec，实施尚未开始。

正式 Spec：[JB-21](https://linear.app/erplus/issue/JB-21/m3-spec原生-series-广告目录识别与无消耗对象查看)，Todo / ready-for-agent，父 Epic 为 JB-19；正文与父级、状态、标签已回读。

## Problem Statement

投手在广告目录查看已有 Smart+ 原生短剧广告时，无法直接辨认它属于哪个 Series、由哪两个身份承载和展示、投放在哪些版位，也不能可靠地沿着广告看到真实的创意及其素材关系。Series、广告组身份和广告/创意身份是不同事实；同一剧名在不同身份下也可能对应不同的 Series ID。

现有目录已经接收并保存平台配置及父子关系，但这些信息主要留在原始配置或折叠字段里。投手不能把“数据可能在 JSON 中”当成可识别的目录，也不能把消耗报表中的创意 ID 当成目录关联事实。缺少字段、只覆盖部分关系或观察时间较早时，页面还应如实显示缺口，而不是把未知补成已知。

## Solution

在现有广告目录的系列、广告组和广告详情中提供原生 Series 识别视图，并从各详情回到父级和查看子级：

- 广告组直接显示 Series ID、广告组投放身份三元组、版位类型、版位及 TikTok 子版位；不把 Series 名称当成必有字段。
- 广告显示父广告组、所属 Series 以及每条 Smart+ 创意的真实 ID、创意身份和实际素材 ID；保留素材名称和缺失原因。
- 相同剧名下不同 Series ID 保持独立，不按名称合并；广告组身份与创意身份分别展示，不默认相同。
- 每个对象显示目录观察时间、来源连接与渠道；字段缺失显示“未提供”，关系缺失按证据显示“未同步”或“覆盖不完整”。原始空字符串或空集合原样保留，不把未知渲染成已确认的空值。
- 继续提供未映射的原始配置。目录识别不创建、修改、复制或启停广告，也不从可见性推断资格、地区、收益或版权。

## Business Flow

N/A。本 MMF 只读取并展示已有目录事实；不新增跨步骤或跨时间保存的业务状态，也不新增失败重试、审批、取消或不可逆副作用。目录同步和发布的既有生命周期不由本 MMF 改写，HTTP 加载状态不等于业务状态。

## User Stories

1. 作为投手，我想从广告组详情看到 Series ID，以便将实际投放对象对应到正确的原生 Series。
2. 作为投手，我想在 Series 名称尚未取得时仍能识别 Series ID，以便不因名称缺失而失去目录识别能力。
3. 作为投手，我想让同名但不同 Series ID 的对象保持独立，以便不把不同身份下的剧目误并为一个对象。
4. 作为投手，我想区分 Series ID 与系列/广告组名称，以免用可重复的展示名称代替平台标识。
5. 作为投手，我想看到广告组的投放身份类型、身份 ID 和授权 BC ID，以便知道谁承载该 Series。
6. 作为投手，我想分别看到广告或创意身份，以便核对展示身份与广告组承载身份是否不同。
7. 作为投手，我想让两个身份及 Series 组合保持原样，以便排查身份选错或授权范围问题，而不是只看到一个被覆盖的 ID。
8. 作为投手，我想看到版位类型、版位和 TikTok 子版位，以便确认广告实际采用的原生投放配置。
9. 作为投手，我想让子版位为空或未提供时如实显示，以免把未返回当成平台不支持。
10. 作为投手，我想从系列详情进入其广告组，再进入广告和创意，以便沿实际层级核对对象。
11. 作为投手，我想从广告详情看到父广告组的 ID 和链接，以便回到所属 Smart+ 结构。
12. 作为投手，我想看到每条 Smart+ 创意 ID，而不只是素材文件名，以便区分同素材的不同创意。
13. 作为投手，我想看到每条创意自己的身份类型、身份 ID 和授权 BC ID，以便定位创意层配置。
14. 作为投手，我想看到创意下的实际平台素材 ID 和素材名称，以便核对创意使用了哪个视频或帖子。
15. 作为投手，我想让目录创意关联独立于消耗报表，以便查看尚无消耗或尚未产生报表事实的已有广告。
16. 作为投手，我想保留视频、帖子和图片等原始素材标识，以免只显示本地文件名而丢失平台事实。
17. 作为投手，我想在素材未完全覆盖时看到缺失关系，以便知道当前列表不是完整创意清单。
18. 作为投手，我想在父广告组未同步时看到明确的未同步提示，以免误以为广告没有父级。
19. 作为投手，我想看到当前目录观察时间，以便判断显示内容是否是近期观察结果。
20. 作为投手，我想看到来源连接和渠道，以便追溯这条目录事实来自哪个授权读取来源。
21. 作为投手，我想让过期或不完整的目录仍标出来源与观察时间，以便带着限制判断，而不是误当实时平台状态。
22. 作为投手，我想在平台未返回某个字段时看到“未提供”，以便区分字段缺失与字段值为空。
23. 作为投手，我想在详情中继续查看未映射原始字段，以便在新增展示投影之外保留排障证据。
24. 作为投手，我想让不同账户和 BC 的同名对象保持各自上下文，以便不会借用其他链路的 Series、身份或素材 ID。
25. 作为无当前账户读取权限的用户，我想得到既有的拒绝结果，以便系统不会因目录识别展示而泄露其他租户或 BC 的对象。
26. 作为投手，我想从未消耗但有目录记录的广告进入详情，以便识别已有原生广告，而不是只能查看产生过消耗的对象。
27. 作为运营复核者，我想用同名不同 Series、不同层身份不同、字段缺失和部分覆盖的目录对象检查页面，以便验证系统没有按名称或默认值推断。
28. 作为运营复核者，我想从页面和接口核对同一对象的 ID、层级和观察时间，以便确认界面展示与目录事实一致。
29. 作为运营复核者，我想明确哪些结论仍未被真实平台目录证实，以免把本地传输夹具或 SDK 字段定义当作生产能力。
30. 作为投手，我希望目录识别只提供查看和导航，以便不会因查看而意外修改、启用或复制广告。

## Implementation Decisions

### 1. 修改 Module：广告目录查询

继续由广告目录 Module 唯一拥有租户、BC、账户授权和已发布对象定位。在现有目录读取 Seam 增加按父广告返回当前创意—素材关系的查询；组合已有目录子对象和创意持久事实，不新增平行目录、Series 缓存或同步任务。

```python
def list_objects(
    session: Session, *, context: TenantContext, bc_id: str,
    advertiser_ids: tuple[str, ...], kind: str,
    parent: EntityRef | None = None,
) -> tuple[AdObject, ...]: ...

def list_native_creatives(
    session: Session, *, context: TenantContext, bc_id: str,
    parent: EntityRef,
) -> tuple[AdCreative, ...]: ...

class DirectoryObjectPublic(BaseModel):
    ref: EntityRef
    name: str
    parent: EntityRef | None
    observed_at: datetime
    published_version: int

class DirectoryObjectPage(BaseModel):
    items: tuple[DirectoryObjectPublic, ...]
    next_cursor: str | None

def read_directory_objects(
    session: Session, *, context: TenantContext, bc_id: str,
    advertiser_ids: tuple[str, ...], kind: str,
    parent: EntityRef | None = None, cursor: str | None = None,
) -> DirectoryObjectPage: ...
```

- `list_objects` 复用现有按父广告读取目录创意配置的 Interface；`list_native_creatives` 只补读取现有创意—素材关系，不引入新的同步或报表 owner。父引用必须是当前租户/账户下的 Smart+ 广告。
- Ensures：两个查询都重新校验读取授权，按真实父广告及 creative ID 精确关联，不按名称或报表事实查找。创意配置与素材关系只按相同 creative ID 对齐；关系缺失由详情查询显式返回，不能称完整。
- Seam：复用现有 PostgreSQL 目录查询 Seam；不新增 Adapter。
- `read_directory_objects` 将已有本地目录读取通过现有 HTTP Module 的只读列表 Interface 暴露给页面。它是本片需要补齐的公开契约，不把已有 Python 查询误称为已有 HTTP 列表。返回真实对象引用、名称、父引用、观察时间和发布版本；分页沿既有资源边界，稳定排序且游标绑定读取范围，每页重新授权。无报表事实也可返回对象，不生成零金额报表行，不触发平台同步。跨页目录发生更新时不承诺整批原子快照，保留各对象版本与覆盖限制。
- 数据形状决定：保留对象原始 `configuration`、父引用、`observed_at`、`published_version` 和来源字段，读取已有创意持久事实。本 MMF 不新增状态标志，也不在已有对象种类判断链中增加 Series 特例；Series 是广告组配置中的标识，不是新的目录实体类型。

### 2. 修改 Module：广告详情查询与契约

现有详情查询增加原生识别投影和来源，不改变报表快照含义：快照用于验证读取上下文，详情仍是当前已发布目录，不宣称是历史目录时间切片。

```python
class DirectoryIdentityPublic(BaseModel):
    identity_type: str | None
    identity_id: str | None
    identity_authorized_bc_id: str | None

class DirectoryMaterialPublic(BaseModel):
    platform_material_id: str
    material_type: str
    name: str | None
    ad_material_id: str | None
    observed_at: datetime | None
    published_version: int | None

class DirectoryCreativePublic(BaseModel):
    creative_id: str | None
    identity: DirectoryIdentityPublic | None
    materials: tuple[DirectoryMaterialPublic, ...]
    observed_at: datetime | None
    published_version: int | None

class DirectoryParentPublic(BaseModel):
    ref: EntityRef
    observed_at: datetime
    published_version: int

class NativeDirectoryPublic(BaseModel):
    series_id: str | None
    series_name: str | None  # 仅平台字段直接提供时返回；不从名称推断。
    group_identity: DirectoryIdentityPublic | None
    placement_type: str | None
    placements: tuple[str, ...] | None
    tiktok_subplacements: tuple[str, ...] | None
    parent_adgroup: DirectoryParentPublic | None
    creatives: tuple[DirectoryCreativePublic, ...] | None
    coverage_reasons: tuple[str, ...]

class AdDetailPublic(BaseModel):
    # 保留既有字段，新增：
    native: NativeDirectoryPublic | None
    source_connection_id: UUID | None
    source_channel: str | None
    published_version: int
```

```python
def get_ad_detail(
    session: Session, *, context: TenantContext, bc_id: str,
    ref: EntityRef, snapshot_id: UUID | None = None,
) -> AdDetailPublic: ...
```

- `native=None` 仅表示当前详情对象类型没有原生广告组投影，不等于平台不支持。对广告组，Series/身份/版位取广告组配置；对广告，父广告组来自目录父引用，Series 和广告组身份取实际父广告组配置，不从广告配置复制。若父组未同步，Series/组身份为未知并保留已有 `parent_not_synced` 原因。
- 原生类型只能依据平台明确返回的目的地标记或 Series 配置判断，不依据名称猜测；证据不足时不把普通 Minis 对象标为原生。无法确认原生类型的对象仍保留通用详情和原始配置；已明确为原生但 Series 字段缺失时显示字段未知。
- 创意 ID 取平台明确返回的 `smart_plus_creative_id`；不得以素材 ID 或报表事实代替。目录子创意与广告原始 `creative_list` 只按相同 ID 对齐，不按数组位置、名称或素材猜测；没有 ID 的创意保留原始配置且 `creative_id=None`。
- 素材关系取目录同步已保存的创意 ID—平台素材 ID 关联，不从报表事实反推。保留素材类型及广告内素材 ID；名称只按完整素材身份匹配，匹配歧义保留未知。没有该 ID 不补造。`creatives=None` 表示对象没有可确认的创意目录，`()` 只有在有完整覆盖证据时才表示无创意；否则保留已观察创意并以 `coverage_reasons` 标明素材补读待完成、关联不完整或覆盖未知。
- Ensures：所有读取沿用当前授权；字段缺失为 `None`，真实空集合为 `()`，原因进入 `coverage_reasons`。响应保留父对象和各创意的观察时间/发布版本，不声称整棵树处于同一原子视图；同步只读取当前已发布对象，未发布子对象不混入。
- Seam：复用现有详情 HTTP 查询和 PostgreSQL 目录读取；没有新网络 Adapter。持久模型不改，因此无需新增数据形状分支或同步标志。

### 3. 修改 Module：广告目录字段映射与详情界面

在广告组“版位与定向”之前增加“原生 Series 与身份”，在广告“内容/广告配置”中增加父组、Series、创意 ID、创意身份及素材 ID。展示仍由同一现有详情 HTTP 响应驱动，不为浏览器增加目录 mock。若需要展示某条创意的详情，复用现有支持 `creative` 类型的详情 HTTP 查询，不把创意变成报表维度。

```ts
type NativeDirectoryView = {
  seriesId?: string
  seriesName?: string
  groupIdentity?: DirectoryIdentityPublic
  placementType?: string
  placements?: string[]
  tiktokSubplacements?: string[]
  parentAdgroup?: DirectoryParentPublic
  creatives?: DirectoryCreativePublic[]
}

export function nativeDirectoryView(
  detail: AdDetailPublic,
): NativeDirectoryView | null

type DirectoryDetailEntry = {
  source: "directory"
  ref: EntityRef
}
```

- 前端仅做展示格式化；Series、身份和父子关系的事实归一化在详情查询 Module 完成。原始 `configuration` 与其他字段继续保留；本 MMF 不要求已映射字段在“其他字段”重复出现。
- 默认展示本 MMF 识别字段，不把 `native_series_id`、组身份、`tiktok_subplacements` 或创意 ID 只留在“其他字段”。字段缺失显示“未提供”，父级缺失显示“未同步”，覆盖不完整显示具体原因，不能用空字符串隐藏。
- 若广告仅有目录记录而没有对应报表行，必须能从广告组目录子广告列表打开详情；不把“有消耗”作为打开目录详情的条件。本 MMF 补一个复用现有目录查询的只读子对象列表，不另建数据 owner。
- 当前报表来源的详情入口保持原样，新增 `DirectoryDetailEntry` 直接以对象引用读取既有详情 HTTP，不先要求 `ReportRow`。在现有账户/广告目录工作区提供可达的目录对象入口及父子导航，根级和子级复用同一只读目录列表契约；即使对象及其父级均没有报表行也能从授权账户范围进入。目录详情不伪造消耗、快照或可执行选择；没有报表上下文时，报表页签明确无数据，管理动作沿原有授权与预览契约，不由本片生成虚假报表行来解锁操作。
- Seam：复用真实广告目录页面 → 现有详情 HTTP → 真实 PostgreSQL 目录对象；不新建浏览器响应拦截或前端数据 owner。
- 数据形状决定：新增一个只读 DTO 投影，不新增与原配置必须同步维护的布尔标志；映射分支按已有对象类型和字段存在性处理，不引入 Series 类型或状态分支。

### 4. 不修改的 Modules

官方 SDK 目录读取、目录归一化/发布、AdObject/AdCreative 持久模型、Series 名称查询和搭建/管理模块保持原状。删除测试：若删除本 MMF 的新投影和界面映射，现有目录同步、搭建和报表仍完整工作，只是用户失去原生识别视图；因此不需要新建目录或 Series Module。

### 5. 边界决定

JB20 的搭建占用覆盖与这里的目录详情读取没有代码或验收依赖，不合并、不阻塞 M3。M1/M2 创建结果可作为目录对象来源之一，但不是识别已有广告的必经条件。生产真实原生对象是否已返回全部字段仍 Unknown；本 Spec 只承诺如实呈现实际返回的目录事实。

## Testing Decisions

### 测试原则与最高 Seam

好测试只断言用户和 API 可见的外部行为：可见 ID、身份、版位、父子/创意/素材关系、来源、观察时间和缺失原因；不断言私有函数、React 内部状态或 JSON 字段恰好经过多少次复制。

唯一主 Seam：现有广告工作区的目录列表、父子导航与详情页 → 同一后端的目录列表/详情 HTTP Interface → 真实 PostgreSQL/必要 Redis → 现有目录同步任务。仅官方 SDK 的 HTTP transport 用可控原生响应替身，浏览器不拦截响应，业务 service 不 mock。沿用现有目录 SDK transport 测试、真实数据库 FastAPI acceptance 和 Playwright 浏览器 HTTP 测试的组合。

### 行为覆盖

1. **已有原生对象端到端**：经目录同步写入一个含 Series 配置的广告组及其广告/创意/素材完整对象，打开真实页面。断言 Series ID、组身份、版位/子版位、父广告组、创意 ID、创意身份和平台素材 ID 默认可见，观察时间与来源和接口一致。错误实现若只返回 raw JSON 折叠、遗漏字段或只显示素材名，应失败。
2. **同名不同 Series**：两个相同业务剧名但不同 Series ID 的广告组分别显示自己的 ID 和身份；任何按名称合并的实现失败。
3. **两层身份不同**：广告组身份与每个创意身份不同且授权 BC 不同，页面同时保留两组事实；把组身份复制到创意或只显示一个 ID 的实现失败。
4. **真实父子与创意关系**：同素材用于不同创意、同创意含多个素材时，仍显示真实 creative ID 和素材 ID；用报表 creative facts 或素材 ID 代替目录关系的实现失败。
5. **缺失与不完整**：分别缺少 Series ID、Series 名称、身份字段、子版位、父广告组、创意关联和素材时，显示“未提供/未同步/未覆盖”的相应事实，不能把缺失变成成功或空值；任何默认借用、按数组位置配对或隐藏原因的实现失败。
6. **部分与过期覆盖**：素材覆盖不完整或目录观察时间早于当前时，页面仍展示已观察事实和限制；声称实时、完整或将缺失补全的实现失败。
7. **租户/BC/账户边界**：无授权、跨租户或跨 BC 的对象请求仍走现有拒绝/404 语义，且不泄露 Series、身份或素材 ID；复用已有授权测试，不把 fixture 账户 ID 变成跨 BC 默认值。
8. **零消耗目录对象**：对象及其父级仅有目录记录、无消耗报表事实时，从现有工作区的账户范围进入目录列表并逐级打开，仍显示识别信息；依赖报表行、report creative ID 或金额事实才能展示的实现失败。分页使用多个父级及重复名称，检查范围隔离、继续读取及权限变化后的拒绝，不以不完整页冒充完整清单。
9. **API 契约**：同一对象经详情接口返回完整原生投影、来源和版本；保留既有原始配置和父级限制。错误地把 snapshot 当历史目录快照或丢掉原始字段的实现失败。
10. **字段映射回归**：扩展现有前端配置测试，验证新字段默认映射且未映射字段仍保留；这些测试是回归网，不代替主 Seam 验收。

### 真实证据与形式验证

本地 MR 证据须注明使用受控官方 SDK transport；不能称为真实 TikTok 生产验收。发布/最终验收还需在明确授权的现有原生广告对象上回读页面与接口，记录真实字段、关系和观察覆盖；无法取得则真实平台支持仍 Unknown，不阻塞代码级识别投影的本地验证，但不能宣称生产已支持。

本 MMF 无新增资金规则、并发写入、重复副作用或不可逆步骤，不需要新增 formal verification skill。现有目录发布的时序与授权测试继续作为前置回归，不把 M2 提交模型套到只读展示上。

## Out of Scope

- 分销及不同输入模式、轮播支持和轮播关系重建。
- 新建/复制广告、广告启停、预算、出价或 ROAS 修改。
- 报表、素材效果、经营结果、收益完整性、赔付或资格判断。
- Series 名称补读、Series 选择/授权资格校验、地区可用性或版权判断。
- 新的目录同步、Series 持久缓存、跨 BC 合并或按名称去重。
- 把创意提升为第四个报表维度，或改写金额/报表事实的 creative ID 语义。
- JB20 搭建占用覆盖修复，以及 M1/M2 的搭建、回读、恢复和取消能力。

## Further Notes

本 MMF 判定：不能在不损失用户结果的情况下拆成两个都可独立上线的有用部分；组层 Series/身份/版位若缺少广告—创意—素材关系，不能完成“识别现有原生广告”的完整结果，而单独展示创意关系又无法识别所属 Series。整体单独上线即可帮助投手核对已有原生广告，因此是一个 MMF。已有能力可复用，但当前默认界面识别仍有缺口，不是“只需验收、无 MR”。Series 名称不构成本 MMF 的独立上线目标，缺名称时以 ID 完成识别。

用户已确认主验收 Seam：现有广告工作区的目录列表、父子导航和详情页 → 真实后端与 PostgreSQL，仅官方 SDK 传输使用替身。root 已完成源码与契约复核，按授权发布到 JB-19 下并应用 `ready-for-agent`。真实生产字段支持、Series 名称是否返回、以及现有原生对象覆盖范围仍须以官方/真实目录证据确认；定稿及本地实施不代表生产验收。
