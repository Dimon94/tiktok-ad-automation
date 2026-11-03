# 2026-11-03 素材新中台凭据迁移与补开户

## 范围与授权

用户提供新开户凭据并授权核验、处理对接；随后明确授权为 xuyin、rongliang 新建账号。目标为新加坡 ACK 集群 c19dbd82590a34b9f87cebb8c1a858796 的 test 命名空间，TK-ADA 入口 https://tkada.shortclipflix.com，素材中台 https://manjumt.gzjunbo.net。

本轮仅调整素材运行配置和指定租户绑定，应用镜像保持 `6b806aa4461efd1f6a02bfdc20de97d3ab592b0f`。未执行数据库结构迁移、素材生产、TikTok 广告操作或历史任务重放。按现行按需备份规则，未创建备份或执行恢复演练；保留本轮配置前值和精确核验记录，不将其称为系统恢复备份。

## 迁移前事实

- 四条绑定均为 ready，使用 default 引用及同一旧凭据指纹。旧 Key 对四个账号签发均返回 INVALID_PROVISIONING_KEY。
- 新 Key 对 xingyu、junbo 签发返回 201，相对 consume 路径及五分钟有效期符合现有客户端。
- 新 Key 对 xuyin、rongliang 返回 404 / ACCOUNT_NOT_FOUND；旧绑定存在旧 company/user ID，不能据 404 认定历史上从未开户。本轮基于用户追加授权新建，未自动把旧请求作为新请求重放。

## 变更

- 通过正式发布锁排除并行发布，使用 Secret resourceVersion 乐观锁仅修改 MATERIAL_TOOL_PROVISIONING_KEYS、MATERIAL_TOOL_DEFAULT_KEY_REF。新增 midplatform-20261103 引用并设为默认，保留旧引用；本机私有 .env.production 同步。
- xingyu、junbo 在绑定行锁事务中核对租户、有效管理员权限、用户名及原指纹，真实签发通过后更新引用/指纹并记录审计。未重新开户。xingyu 的旧 company/user/公司 Key 字段仅保留旧系统事实，尚未核验在新中台的连续性，不得据此调用新生产 API。
- 用户授权后为 xuyin、rongliang 使用原公司名和用户名，各生成独立随机素材侧密码及新操作固定幂等键；两次开户均为 201 / replayed=false。没有用旧开户幂等键、没有改 TK-ADA 登录密码、没有遇冲突改名或覆盖远端账号。
- 两个新开户响应包含 company/user/apiKey/key/replayed，与现有客户端字段约定匹配；公司交付固定 r2、pushAdPlatform=true。行锁事务核对原绑定及管理员后，保存新 company/user/apiKey ID、加密公司 Key 与新凭据引用；旧 ID 留在迁移审计，消费原消息代数，不触发再次开户。
- 逐角色正常重建以读取新配置，保留现有退出宽限、镜像、资源、并发和四个业务开关；Beat 使用原 Recreate 策略。

## 实际核验

- 四个账号重新签发：均 HTTP 201，固定相对 consume 路径、单 token、有效期 300 秒。
- 新建两公司的公司 Key 分别读取 /api/strategy-catalog、/api/material-library-production/capabilities：四次均 HTTP 200。仅证明接口权限和返回可读，未提交素材订单，不代表成片与交付联调完成。
- 真实浏览器从 TK-ADA 各原租户菜单点击素材制作，检查新中台公司归属；结果见下表。

| 租户 | 处理 | 浏览器验收 |
| --- | --- | --- |
| junbo | 原账号凭据迁移 | 已进入 /create，显示当前账号归属：骏伯 |
| xuyin | 新中台新建并绑定 | 已进入 /create，显示账号及公司 xuyin |
| rongliang | 新中台新建并绑定 | 已进入 /create，显示账号及公司 rongliang |
| xingyu | 原账号凭据迁移 | 已进入 /create，显示账号及公司：星屿映画 |

xuyin、rongliang 两个新公司页面均提示“本公司尚未配置平台”，需素材中台管理员按该公司的真实授权配置内置取源平台；未复制其他公司的平台凭据或扩大权限。TK-ADA 当前没有平台配置接口权限，开户和登录成功不代表公司取源配置已完成。

## 私有证据

本轮私有目录为工作区 `.local/material-migration-20261103/`，含凭据安全文件、仅本轮配置字段前值、固定新开户正文及响应、行锁事务执行结果、只读接口检查和浏览器截图。全部不进 Git，正文、工具输出及提交不包含 Key、密码、Cookie 或一次性登录链接。

- 四个租户经生产现有 create_login_link 服务（包括本地租户权限、绑定状态、凭据指纹与上游响应校验）全部通过，审计已提交。
- TK-ADA HTTPS 首页与 /api/utils/health-check/ 均返回 200。
- 本轮没有应用行为代码变更，未重复运行无关全量测试；私有操作脚本通过 Python 语法检查，文档执行 git diff --check。浏览器未提交任何生产订单。

## 最终运行验收

11 个角色均已实际加载 midplatform-20261103 默认引用及同一新凭据指纹，全部就绪；容器镜像、资源、运行参数和退出宽限与变更前逐项相等。四个业务开关仍全部为 true。11 个 Running/Ready Pod、唯一 Beat 已回读核对，正式发布锁已在核对本轮 UID 和 token 后释放。

四个浏览器登录验收均从 TK-ADA 原租户素材制作菜单发起，确认新中台 /create 页面公司归属正确；没有把单纯签发成功当作浏览器登录成功。账号身份切换后以各次页面实际归属为准，未操作旧用户页的生产按钮。
