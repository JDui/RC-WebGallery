# RFC：小红书个人点赞订阅与 NAS 登录态持久化

> 状态：**设计提案（未实施、未验证真实账号）**  
> 适用项目：BiliGalleryRC / JDui/bili-gallery，现有 v10.4 架构  
> 日期：2026-10-19  
> 范围：**本人已点赞**的小红书图文／视频笔记；不包括他人点赞、收藏夹、自动点赞或发布操作。

## 0. 结论和决策

本提案采用 **「站点源适配器 + 独立的 XHS Browser Worker + NAS 私有浏览器配置目录」**。首次由账户所有者在临时、受保护的远程浏览器中手动扫码；后续任务复用该目录中的 Chromium 登录态。FastAPI 主应用仅接收经过净化的笔记清单，不接触小红书 Cookie 或浏览器 Profile。

但须满足先决条件：小红书当时的网页功能、授权边界及平台规则允许该访问方式，且用户能在自己的浏览器中稳定看到并枚举自己的点赞列表。历史公开代码仅能证明**过去存在相关接口**，不构成当前可用性保证或平台许可。未通过验证时本功能保持关闭，不应承诺「无人值守自动订阅」。

分阶段交付：
1. **P0 可行性／合规门禁**：用户授权环境、手动浏览器确认分页与媒体、检查平台规则和实际风险；不写入生产数据库。
2. **P1 低风险基线**：浏览器手动导出净化 manifest → NAS 导入／去重／下载／归档；不需 NAS 存储 XHS 会话。
3. **P2 可选 NAS 自动模式**：独立 Playwright Chromium Worker + 持久化登录态 + 任务中心／定时同步；只在 P0 通过且用户显式启用后交付。
4. **P3 增强**：按源展示、同步审计、重新登录 UI、媒体验证与可靠恢复。

## 1. 与当前仓库的集成位置

已核对当前主分支：
- docker_app/app/main.py：FastAPI 入口及服务绑定；
- docker_app/app/db.py：SQLite WAL；已有 subscriptions、site_sources、site_posts、site_assets、folders、assets、task_runs；
- docker_app/app/services/site_syncer.py：站点发现、过滤、素材下载及站点同步任务；
- docker_app/app/services/site_parser.py：现有 RSS/Sitemap/HTML 页面数据模型 ParsedPost / ParsedAsset；
- docker_app/app/services/site_downloader.py：流式写入临时 .part 后原子改名；
- docker_app/app/services/scheduler.py：APScheduler，站点同步间隔；
- docker_app/docker-compose.example.yml：源码模式，仓库整体挂载到 /workspace；
- docker_app/docker-compose.nas-amd64.yml：NAS 模式，持久化 /storage/config、/storage/data；当前主容器配置为 root 运行。

**关键约束**：

- 不把小红书登录态放进已有 auth_state（该表专用于 B 站）或 config.json；不混用 B 站二维码登录组件。
- 不把 xhs_likes 当成通用 RSS/HTML 页面交由 SourceParser 去解析；实现独立的 XhsLikesAdapter，通过规范化 DTO 接入现有站点／索引管线。
- 不直接调用现有 execute_full_validation() 处理小红书：它会先清空站点内容及媒体文件，不能用于部分扫描或登录失效的点赞源。
- 普通网站的基于发布时间的增量截断不适合点赞源：**旧笔记可能今天才被点赞**；必须以本地首见状态／扫描结果去重。

建议模块划分（规划路径，不代表已创建）：

~~~text
docker_app/app/services/xhs/
  models.py             # Manifest / Note / Asset DTO 和验证
  adapter.py            # 私有点赞源 -> 标准站点模型的转换
  sync.py               # 快照、增量、去重、失败恢复
  media_resolver.py     # 优选素材候选地址；不承诺原件
  worker_client.py      # 仅访问 Docker 内网 worker 的客户端
docker_app/app/routes/xhs.py       # 源管理、状态与操作接口（或并入 main.py）
docker_app/xhs_worker/             # 独立容器：Playwright + Chromium
docker_app/tests/test_xhs_*.py
~~~

## 2. 目标架构和信任边界

~~~mermaid
flowchart LR
    U[用户：一次性扫码／必要时重登] --> V[临时浏览器登录界面]
    V --> W[隔离的 XHS Worker]
    P[(NAS 私有 Profile 卷)] <--> W
    S[APScheduler / 任务中心] --> A[XHS 订阅适配器]
    A <-->|内网受认证的任务协议| W
    W -->|净化 Manifest，不含 Cookie| A
    A --> D[(SQLite：站点／点赞状态／审计)]
    A --> M[媒体候选解析／下载／校验]
    M --> G[现有 Gallery 存储／索引／缩略图]
    B[本机浏览器 JSON 导出] --> A
~~~

**安全边界**：仅 XHS Worker 有权访问持久化 Chromium Profile；主应用没有该数据卷的读权限。所有浏览器响应中的 set-cookie、authorization、xsec_token、签名查询串、二维码信息均不得进入 manifest、数据库、日志或任务错误详情。特殊短时地址／令牌如运行确实必需，只允许驻留 worker 内存或短期受保护的工作区，任务完成即清理。

Worker 与主服务通过**仅 Docker 内网可访问**的端点交换任务与净化结果；引入只读 Docker Secret（例如 /run/secrets/xhs_worker_token）进行双向身份校验，不将 token 放在前端 JS、镜像、Compose 明文或数据库。主应用不能暴露任意导航 URL、任意下载 URL 或接收未校验的文件路径，防止 SSRF／路径穿越。

## 3. NAS 登录态持久化设计

### 3.1 存储布局

建议 NAS 宿主目录独立于已有图库存储：

~~~text
<NasAppRoot>/
  config/                     # 现有 /storage/config（app.db 等）
  data/                       # 现有 /storage/data（图库）
  xhs-private/                # 新建：仅 xhs-worker 挂载
    chromium-profile/         # Playwright user_data_dir
    work/                     # 可删除的临时任务状态
~~~

Compose 中为 Worker 单独映射示意（**概念配置，不是当前仓库已有可运行服务**）：

~~~yaml
services:
  xhs-worker:
    # 需另行实现并固定版本的镜像与浏览器依赖
    # 必须以非 root 运行，配置合适 UID/GID、shm_size 及浏览器沙箱
    volumes:
      - /your/nas/private/bili-gallery/xhs-private:/var/lib/xhs:rw
    networks: [xhs-internal]
    # 正常运行不暴露宿主机端口，也不挂载主应用的 config/data

networks:
  xhs-internal:
    internal: true
~~~

**网络拓扑注意**：internal 网络禁止直接访问外部小红书。实际 worker 需同时加入一个有受控出站能力的网络；主应用只通过 xhs-internal 的专用地址访问 worker。禁止以 network_mode: host 解决连通性。容器重建后 volume 仍保存数据；必须保持 UID/GID 稳定、同一 Chromium 版本族兼容，并避免 NAS 清理任务清除目录。定期加密备份 Profile；需要恢复时应先停 Worker 再完整恢复。

Playwright 采用 launch_persistent_context(user_data_dir="/var/lib/xhs/chromium-profile", ...)；这一配置保存浏览器 Cookie、LocalStorage 等会话状态。Profile 是**高敏感认证凭据**，不可提交 Git、上传截图、放入媒体根目录或通过 HTTP 文件服务开放。Chrome Profile 会随浏览器版本发生迁移，升级前备份、升级后回归验证。Profile 仅允许一个 Chromium 实例占用：Worker 用进程锁，禁止定时任务与扫码登录同时启动同一 Profile。

### 3.2 首次登录与重登录

- 在「设置 → 小红书（规划）」点击「启动手动登录」，状态从 NOT_CONFIGURED → LOGIN_REQUIRED → LOGIN_IN_PROGRESS。
- Worker 暂停相关抓取任务，临时启动带显示服务的 Chromium（Linux 容器可使用 Xvfb + 经过认证的 noVNC/远程桌面）。**默认不得开放 0.0.0.0:6080**。
- 推荐仅把临时登录入口绑定 NAS 的 127.0.0.1:6080，通过 SSH 端口转发访问；或通过已有的带登录保护、TLS 的内网反向代理暴露，设置短时有效的一次性访问令牌。禁止公网裸露 noVNC。
- 用户在真实小红书页面内自行完成手机扫码或官方交互验证，不读取账号密码、不抓取二维码原图、不自动处理验证码。
- 完成后 Worker 校验登录后的用户身份及可访问的本人点赞页，将账号标识的最小化摘要及 checked_at 回传。关闭远程可视会话、撤销登录入口和一次性令牌。
- 后续定时执行无头浏览器读取**同一个 Profile**。若登录失效，任务进入 NEEDS_REAUTH，并提示再次走上述流程；**不自动重新登录、不过风控验证**。
- 切换账号必须显式退出并清除原 Profile（经用户二次确认），清理前保留既有归档数据但把账号对应的同步源禁用；避免跨账号混库。

状态机：DISABLED → LOGIN_REQUIRED → LOGIN_IN_PROGRESS → READY → RUNNING → READY；可进入 EXPIRED、NEEDS_MANUAL_VERIFICATION、RATE_LIMITED、PAUSED、ERROR。Worker 进程崩溃与 401/403/429、平台自定义限制码必须按类别报告；不得在日志回显请求头或完整响应。

### 3.3 为什么不直接把 Cookie 存 SQLite

- 仅 Cookie 可能缺少 LocalStorage/IndexedDB 或其他浏览器状态，且 Cookie 拼接调用私有接口易受网页版本变化影响。
- 现有 auth_state 已用于 B 站，不宜复用其字段及明文存储方式。
- Profile 隔离便于通过文件系统权限、卷隔离和凭据轮换管理；不会随 app.db 备份无意扩散。
- 仍可支持**手动导出 manifest 模式**，用户选择不在 NAS 留存 XHS 登录态时可关闭 Worker。

## 4. 点赞列表发现与规范化

公开历史实现 ReaJason/xhs 的 get_user_like_notes 使用过 GET /api/sns/web/v1/note/like/page（user_id、num、cursor），XHS-Downloader 曾提供点赞页链接提取脚本。这些仅作为**调查线索**，不可在实现中写死“当前可用”。首次进行一次用户授权下的浏览器观测：能否访问本人点赞、分页游标、历史翻页、笔记详情、图文／视频／Live Photo 元数据。严禁绕过 CAPTCHA、设备指纹验证及平台限制。

建议统一 manifest v1：

~~~json
{
  "schema_version": 1,
  "source_kind": "xhs_my_likes",
  "owner_key": "opaque-local-account-id",
  "scan_id": "uuid",
  "started_at": "2026-10-19T09:00:00+08:00",
  "completed_at": "2026-10-19T09:03:00+08:00",
  "complete_snapshot": false,
  "notes": [
    {
      "note_id": "example-note-id",
      "url": "https://www.xiaohongshu.com/explore/example-note-id",
      "title": "示例",
      "author_id": "example-author",
      "type": "image",
      "published_at": null,
      "observed_at": "2026-10-19T09:02:00+08:00",
      "assets": [
        {"ordinal": 0, "kind": "image", "candidate_ref": "ephemeral-worker-ref"}
      ]
    }
  ]
}
~~~

以上是**约定结构，不是当前网页 API 的响应**。owner_key 为本地不可逆／不含 Cookie 的账户标识。Manifest 最大体积、条目数、字段长度均须限制，校验域名和 note_id 格式，拒绝含敏感认证键的嵌套 JSON。candidate_ref 只允许 worker 内部解析，不能直接暴露需要鉴权的真实素材 URL 或令牌。对本机手动导出的 JSON，优先不含媒体候选，改由确认后的媒体解析流程处理。

去重键至少为 (source_id, note_id)，媒体去重键为 (source_id, note_id, ordinal, kind, fingerprint/稳定资产键)。分页顺序、发布时间、首次观察时间互不等价：
- published_at = 原作品发表时间；
- first_seen_at = 首次在本人点赞订阅中看到；
- last_seen_at = 最近一次成功扫描仍看到；
- liked_at = **未知**，除非有可靠源提供，不得从 published_at 或 first_seen_at 伪造。

## 5. 数据模型与同步语义

保持 site_sources/source_type='xhs_likes'、site_posts、site_assets、folders/assets 的兼容；建议新增版本化迁移表：

| 表（拟新增） | 关键列 | 作用 |
| --- | --- | --- |
| xhs_source_accounts | source_id PK/FK、owner_key、auth_mode、auth_status、last_auth_check_at | 仅存非敏感账户摘要与状态；不存登录凭据 |
| xhs_like_state | source_id、note_id 联合 PK、site_post_id、first_seen_at、last_seen_at、is_currently_liked、last_full_snapshot_id | 作品已点赞状态；取消点赞不删除图库 |
| xhs_sync_runs | scan_id PK、source_id、started_at、finished_at、result、complete_snapshot、seen_count、error_category | 同步审计、快照完整性及故障定位 |

可为 site_posts 增加平台 note_id 的独立映射；不得仅依赖可变分享 URL 去重。现有 site_sources 的 entry_url/source_type/selector 字段可保留，用受控的适配器分支处理，不将凭据或自定义任意脚本注入 selector。

同步事务步骤：

1. 取得**单源串行锁**，检查用户启用开关、合规门禁、Worker READY 状态、磁盘空间、媒体配额。
2. 分页发现并逐页写入 staging/运行内存；具备最大页数／最大时长／取消处理，记录游标但不持久化敏感查询串。
3. 校验每条 note_id、URL、媒体类型；记录 seen，按主键 UPSERT，首见笔记进入下载队列。**不得因为发布日期早于上次同步就跳过新点赞**。
4. 媒体获取、过滤、审核、索引、缩略图复用现有组件；失败项保留可重试状态，已完成媒体不重复下载。
5. 仅当扫描从第一页连续到明确结束、无丢页／限速／中断，且 complete_snapshot=true 时才允许把本次未出现的既有 note 标记为 is_currently_liked=false。这个标记仅描述当前订阅状态，不触发文件删除。
6. 游标异常、页面结构变化、用户取消、登录失效、部分同步、超时一律 complete_snapshot=false；**绝不将未出现的笔记认定为取消点赞**，更不执行破坏式清库。

事务须支持任务重试（同一 scan_id 幂等）以及 crash/restart 恢复。数据库写入、媒体落盘和图库索引允许最终一致，但必须能识别未完成的 .part 文件，避免孤儿媒体。全量校验使用只读对比／增量修复；单独设计「显式清空此源本地归档」高风险操作，不复用先删后建的站点全量校验。

## 6. 无可见水印媒体的候选解析

目标是尽可能保存**无平台覆盖水印的可用媒体版本**，而非承诺一定获得作者的上传原件或固定最高码率。必须确保访问权、版权与平台规则符合实际使用条件。

- 图文：采集笔记可见图片序列，保留原始比例、尺寸、顺序及封面映射；优先经验证的高清候选，失败回退网页可访问版本。
- 视频：历史开源实现常通过 video.consumer.originVideoKey / origin_video_key 等字段探索源视频候选，或从 stream.masterUrl、backupUrls 选择可播放版本。字段和 CDN 路径**均为未经当前站点验证的研究线索**，不可视为稳定 API，也不保证无水印。
- Live Photo：只有源页面明确有相应动态媒体时才分别处理静态封面与动态资源；必要时延后支持，不凭空构造视频。
- 可能的签名 URL、Referer、Cookie 不能落库为长期 metadata；由 worker 当次解析／下载，过期后重新解析。不得向任意外站透传 Cookie。
- 下载前执行目标域白名单、DNS/IP 解析检查（含重定向后）、内容长度上限、MIME／文件魔数、磁盘配额与视频可解码性校验；拒绝 SSRF、内网地址、HTML 错误页伪装图片以及路径穿越。
- 使用临时 .part 和原子落盘，记录 sha256、像素尺寸、视频时长及实际保存版本信息；原始笔记和作者链接保持可追溯。不要把有水印的备选自动命名为 original。

## 7. 服务接口建议（草案）

| 主应用接口（拟新增） | 作用 | 约束 |
| --- | --- | --- |
| GET /api/xhs/status | 查看认证状态、上次同步、剩余失败数 | 不返回 Cookie/Profile 路径/敏感 URL |
| POST /api/xhs/login/start | 发起一次性手动登录 | 管理员权限 + CSRF 防护 + 登录互斥 |
| POST /api/xhs/login/revoke | 注销／删除登录 Profile | 二次确认；不删已下载图库 |
| POST /api/xhs/sources | 创建本人点赞订阅源 | 仅允许当前已登录的本人 |
| POST /api/xhs/sync | 手动同步 | 幂等 job id、单源锁、限流 |
| POST /api/xhs/manifest/import | 手工 manifest 导入 | 文件尺寸、schema、账户归属校验 |
| GET /api/xhs/runs | 查询同步日志和失败类别 | 严格脱敏 |

这些路径和认证机制是**设计草案**，实现阶段需匹配现有 main.py 的实际 API 命名、鉴权、任务队列模式；不得据此认为现有版本已具备这些端点。Worker 内部 API 仅暴露在容器内网并独立认证；不得向前端开放导航、任意 Fetch、Cookie 导出接口。

界面可在「订阅管理 → 小红书 → 我的点赞」新增来源卡片，显示「已登录／需扫码／风控暂停／部分扫描」、「上次完整同步」、「本次新增」、「下载失败」；提供立即同步、仅重新检查、手工导入及重新登录。登录会话页面必须提示浏览器会话具有账户访问权限。

## 8. 资源和调度

- Worker 不与现有图库主进程共用 Chromium Profile 或 SQLite 连接，单账号任务串行。
- 默认不开启自动同步；由用户显式授权并配置间隔。计划初期采用低频、有限分页、单任务下载，按实际平台规则及实测表现调整，不设置任何试图规避限制的速率或代理轮换。
- 使用 APScheduler 发起任务，但让任务中心控制暂停／取消／重试并遵循当前前台浏览优先级。
- 内存约束：批量分页以流式写入及 staging 承载，禁止一次性保存全部原始 HTTP 响应。
- 磁盘空间不足、媒体配额达到上限时停止下载，保留已索引笔记和可恢复队列。
- 升级 NAS 镜像不应清除独立 Profile；浏览器升级采取明确的版本锁定、迁移前备份和回退路径。

## 9. 风险、限制及停机策略

| 情况 | 系统动作 |
| --- | --- |
| Cookie 失效／用户退出 | NEEDS_REAUTH，停止自动请求；手工扫码后恢复 |
| 需要验证码／设备安全校验 | NEEDS_MANUAL_VERIFICATION，暂停；不自动绕过 |
| 429 / 403 / 平台限制 | RATE_LIMITED / PAUSED；止损、指数退避、人工检查；不轮换账号/IP |
| 仅抓到部分页或网页结构变化 | PARTIAL；保留已发现内容、禁止取消点赞判断 |
| 下载地址过期或返回错误 | 单资产可重试；重新合法解析候选，不保存长期签名 URL |
| 浏览器进程崩溃／NAS 重启 | 清理孤儿锁、恢复 pending job，不重复下载，不丢已完成扫描日志 |
| 用户撤销点赞 | 仅在完整快照时变更 is_currently_liked，不自动删除本地已归档作品 |
| 平台规则禁止该方式 | 关闭浏览器自动模式；优先保留用户自愿提供的合法导入途径 |

平台服务条款（包括禁止未经授权自动抓取／下载的条款）可能限制本功能，私人账户不当然构成平台授权；同时需尊重作者版权与隐私。**不得实现验证码绕过、伪造设备指纹、签名算法对抗、IP 轮换、批量多账号采集。** 若 P0 发现无法合规采集，P2 不进入开发。

## 10. 验收与测试门槛

### P0 Go / No-Go（真实账号且所有者知情授权）

- [ ] 手工登录后能可靠显示本人点赞页（非收藏页、非自己获赞通知）。
- [ ] 已确认账号切换、列表分页／cursor、has_more／结束信号与历史内容覆盖；记录结构变化证据。
- [ ] 至少 30 条包含多图、视频、（若存在）Live Photo 的代表样本，验证每个资源的尺寸、顺序、可播放性和**可见水印情况**。
- [ ] 验证容器／NAS 环境的访问性、DNS、代理和媒体权限；不用绕过任何平台访问保护。
- [ ] 评估服务条款、频次限制及单账号可接受风险，并由用户决定是否启用。
- [ ] 验证登录态关闭／重启浏览器后复用及失效重登；记录真实会话寿命，不作永久有效承诺。

任何关键项未通过 → P2 NO-GO；允许 P1 手工导入作为替代，但也需遵守适用规则。

### P1/P2 单元／集成测试

- [ ] Manifest 版本、不可信字段、重复 note_id、恶意 URL、过大 payload、路径穿越及敏感字段拒绝。
- [ ] “旧作品今天点赞”、同一作品重复扫描、笔记 URL 改变、视频/多图顺序变化均正确去重。
- [ ] complete_snapshot=false、扫描取消、分页断层、429 时永不标记取消点赞／删除文件。
- [ ] 完整快照取消点赞仅改变订阅状态，不误删图库文件／收藏标记。
- [ ] 断电／任务取消后 .part、SQLite WAL、worker profile 锁恢复，任务重试幂等。
- [ ] 登录状态、二维码、Cookie、请求头、xsec_token、签名地址不出现在 app.db、日志、浏览器前端 API 或错误详情。
- [ ] 同源并发互斥、切换账号隔离、跨源不污染，以及可关闭 / 可回滚。
- [ ] 测试使用合成 manifest 和伪造下载服务；CI 不访问真实小红书，也不使用个人凭据。
- [ ] 容器镜像固定版本、非 root Worker、Profile 卷文件权限与备份恢复验证。

## 11. 执行计划和改动边界

**阶段 0 — 验证**：评估权限与网页行为；形成实测兼容矩阵和 Go/No-Go 记录，当前没有完成。

**阶段 1 — 数据接入**：Schema migration、XhsLikesAdapter、manifest import、任务日志、增量快照及合成测试；先不引入任何 Cookie 存储。

**阶段 2 — Worker**：固定版本的 Playwright+Chromium 容器、分离数据卷、独立认证、扫码登录、会话状态机、无头续用、NAS Compose 可选 profile；做权限和风险校验。

**阶段 3 — 媒体和 UI**：候选媒体解析、下载质量追踪、站点订阅页、任务中心、错误恢复及文档；最后再考虑是否开放定时任务。

**降级／回滚**：禁用 xhs_likes 来源和 Worker 即停止新采集，保留其他站点功能与已入库文件；Schema 迁移应向后兼容，卸载 Worker 不影响 B 站现有认证。不要在自动回滚时清除 Profile；注销或删除需单独确认。

### 本 RFC 不包含

- 可运行的 XHS 私有 API 代码、Cookie、二维码凭据、浏览器 Profile 或下载脚本；
- 已验证的无人值守运行结果和所谓「100% 无水印」保证；
- 对现有数据库、Compose、Python 代码、UI、GitHub Actions、依赖和版本号的实际修改。

## 12. 参考与核验说明

- [Playwright Python：BrowserType.launch_persistent_context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context) — 持久化用户数据目录。
- [Playwright：Authentication](https://playwright.dev/docs/auth) — 认证状态的敏感性，禁止提交仓库。
- [ReaJason/xhs：core.py](https://github.com/ReaJason/xhs/blob/master/xhs/core.py) — 历史 get_user_like_notes 入口，仅作研究线索。
- [JoeanAmier/XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader) — 历史点赞链接提取与媒体下载功能；注意开源协议和平台风险。
- [小红书用户服务协议](https://agree.xiaohongshu.com/h5/terms/ZXXY20220331001/-1) — 上线前重新查验有效版本。

**现状声明**：此文档是基于现有仓库结构与公开研究的设计决策记录，**未在当前小红书真实账号上完成端到端验证**。所有新文件路径、接口、数据库表及部署片段均为待实施方案。
