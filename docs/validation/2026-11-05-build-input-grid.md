# 2026-11-05 广告搭建录入改造（本地完成，未发布）

> 2026-11-05 最新状态：已随 c209df37 发布生产，公网表格资源与素材标识合同已核验。下文保留当时的本地开发验证，生产结果见 [整批发布](2026-11-05-build-input-production.md)。

## 用户确认与实现

以用户确认的第二版原型为准，详见 [设计](../superpowers/specs/2026-11-05-build-input-grid-design.md)。主页面保留剧名/账户直接粘贴；明确素材类型单选；取链参数压缩到配置区且按需展开，缺支付模板直接提示；策略摘要提供详情。删除账户覆盖和 BC 授权长说明。

“填写更多剧目信息”打开大弹窗，五列依次为剧目名称、素材标识、推广链接、剧目 ID、归因名称。支持单列/矩形粘贴、Excel 引号、空列、文本编号前导零、自动加行、撤销、逐格编辑和行列定位校验；取消不改输入。超出五列或 1000 行直接拒绝，不静默截断。

素材标识独立于链接持久化，空 URL 继续自动取链。视频、图片都按非空标识做字面包含匹配、忽略大小写；空标识按剧名，零结果不回退。第二步补链保留标识；草稿恢复、重新排序和重新准备保持对应关系。准备页展示匹配依据。

## 实际验证

采用独立本地 PostgreSQL 18 和 Redis 7 测试容器，无真实广告/版权方写入。

- 后端：`uv run --frozen pytest tests/modules/builds/test_drama_details.py tests/modules/builds/test_drama_image_identifier.py tests/modules/builds/test_manual_links.py tests/modules/builds/test_drafts.py tests/modules/builds/test_image_composition_service.py -q --tb=short`：**88 通过**。验证无链接自动取链及结果回写、标识字面匹配与零结果、图片冻结匹配依据、补链保留标识等。
- 网格纯函数：`bun test unit-tests/dramaGrid.test.ts`：**6 通过、16 断言**。覆盖五列顺序、ID 前导零、空列、矩形/单列、越界、1000 行上限、条件必填和重复/无效字段。
- 浏览器：`PLAYWRIGHT_PORT=5186 bun x playwright test tests/build-preparation.spec.ts tests/build-link-config.spec.ts tests/build-preview.spec.ts --project=workspace --workers=2 --reporter=line`：**134 通过、1 个基线失败**，详见下节。之后新增“恢复无链接的素材标识”用例，与桌面/窄屏网格用例合跑 **2 通过**；共 135 个不同的相关页面用例通过。
- TypeScript、正式与演示生产构建 `bun run build`、针对修改文件的 Biome/Ruff、`git diff --check` 通过。
- 已查看 1440px 主页面、1440px/390px 弹窗截图，确认窄屏横向滚动仅发生在表格内，底部确认按钮可见。
- 独立只读审查未发现 Critical/Important；审查者未运行测试，上述测试由实施会话执行。

## 已复现的基线失败

`build-preview.spec.ts` 的“原提交404保留同request且切BC先守卫，不把未找到当未创建”期待标题“提交结果尚待核实”，而当前工作区离开确认框显示“有未保存的内容”。在本轮工作树单独运行失败；将 **HEAD 未修改的前端**导出到独立目录、另起 Vite 端口后单独运行，也出现相同失败。本轮不修改无关的工作区守卫/提交行为，不将该项计为通过。

## 数据与发布边界

复用 DraftInput.manual_link JSON，未新增数据库列/迁移。`ManualLinkInput.url` 现在允许空字符串，并新增选填 `material_identifier`。这改变了输入 JSON 的业务语义，发布时需要 API 与所有处理搭建的 Worker 同版本切换、正常结束旧任务，不能仅部署前端或让旧 Worker 把无 URL 的补充信息当手动链接；图片快照继续冻结实际匹配文本，既有快照保持原依据。按部署合同变更流程审查具体发布方案。

未推送、未发布、未修改服务器/线上配置，未创建真实广告，也未进行外部业务联调。本地构建与模拟接口测试不代表生产验收。
