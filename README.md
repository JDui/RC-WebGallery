# RC-WebGallery｜多平台媒体采集与个人内容归档

RC-WebGallery 是一个面向个人收藏与自托管场景的多平台媒体采集、归档和 Web 图库管理项目。项目从 B 站动态相簿演进而来，但不再局限于 B 站：目前支持 B 站动态与 Live Photo、小红书本人点赞笔记，以及通过 RSS、Sitemap 或 HTML 列表页配置的通用站点来源。各来源汇入统一的任务队列、SQLite 索引和图库界面，可在 Docker Compose 环境中部署到个人服务器或 AMD64 NAS。

> **来源范围说明**：B 站支持 UP 主动态媒体；小红书功能仅面向本人账号的点赞列表归档，不是任意用户抓取器；通用站点源取决于页面结构和可访问内容。平台页面结构、登录验证与资源地址可能变化，因此应将采集视为个人备份工具，而非官方 API 的稳定替代。

## 功能特性

- **动态与媒体采集**：支持 B 站九图动态和 Live Photo 拉取，保留动态正文、发布时间及来源信息；根目录的 <code>bili_9pics_downloader.py</code> 与 <code>LivePhoto/</code> 目录保留对应的采集脚本。
- **相簿浏览**：按动态或图片视图浏览图库，支持按时间、年份、月份、来源类型和订阅筛选；可按标题、正文或订阅源搜索，并将动态加入收藏。
- **最近更新**：把近期新增内容按动态聚合，直接查看本次同步带来的文件、图片和 Live Photo 变化。
- **重复检测**：对图库图片进行相似内容检测，支持查看重复组、忽略重复组、选择保留项，以及将不需要的动态放入垃圾桶。
- **审核与过滤**：对命中过滤规则的动态进入待审核列表，并保留过滤日志；审核项可以查看原始动态、放行下载或永久忽略。关键词和长图等规则可在设置中调整。
- **任务队列**：拉取、审核下载、校验、页面索引、缩略图重建、重复内容处理、垃圾清理和站点同步等工作统一进入任务中心，可查看进度和日志，支持暂停、继续、取消与重试。
- **订阅与站点源**：管理 B 站 UP 主订阅和外部站点订阅；站点源支持 RSS、Sitemap 或 HTML 列表页，并可配置定时同步、筛选规则和网络代理。
- **小红书个人点赞**：内置浏览器扫码与持久化登录，支持本人点赞笔记扫描、图文/视频/Live Photo 媒体解析与归档、失败重试和手工清单导入；首次拉取条数默认 100，可设 1–10000 或不限，锚点数默认 3、可设 1–20。固定直连，刷新周期独立设置，每轮随机前后抖动 1–22 分钟；图库提供独立的小红书来源筛选入口。首次使用需完成本人账号、分页与访问规则确认。
- **登录与运行控制**：支持 B 站二维码登录、Cookie 状态检查和退出登录；可以配置定时拉取间隔、站点同步间隔及过滤关键词。
- **缩略图与性能调度**：为原图生成统一 thumb 缩略图（默认短边 368px、WebP 质量 50，沿用 small 存储路径），配合页面按需加载和本地索引；前台浏览拥有更高优先级，后台拉取、清理、索引和重复检测会主动让行。

## 效果展示

以下图片均由 <code>docker_app/scripts/seed_review_data.py</code> 生成隔离的占位图片、视频和 SQLite 数据后截取。它们不读取仓库中的 <code>storage/</code>，也不包含真实账号、二维码、订阅或外部服务内容。

![图库浏览与筛选](docs/images/gallery.png)

图库首页展示动态卡片、Live Photo 标记、收藏入口、来源筛选和智能相簿导航。

![媒体查看器](docs/images/viewer.png)

查看器支持上一张、下一张、删除和关闭等操作；图中媒体为占位图片。

![待审核页面](docs/images/review.png)

待审核页面展示命中过滤规则的示例动态，可选择查看原始动态、放行下载或永久忽略。

![任务队列页面](docs/images/tasks.png)

任务队列集中显示当前状态、后台任务、时间和日志详情入口。

## 部署指导（Docker Compose）

> **提示**：如果你不熟悉 Docker 或不想处理部署细节，可以将本项目地址发送给你的 Agent，请它协助完成部署。

### 源码构建启动

需要 Docker Engine 和 Docker Compose v2。命令从仓库根目录执行：

~~~bash
mkdir -p storage/config storage/data
docker compose -f docker_app/docker-compose.example.yml up -d --build
~~~

应用启动后访问 <http://localhost:7860>。源码 Compose 文件会把仓库挂载到容器 <code>/workspace</code>，运行时数据保存在仓库的 <code>storage/</code> 下。

常用运维命令：

~~~bash
# 查看容器状态
docker compose -f docker_app/docker-compose.example.yml ps

# 查看最近 200 行日志并持续跟踪
docker compose -f docker_app/docker-compose.example.yml logs -f --tail=200

# 升级源码并重新构建
git pull
docker compose -f docker_app/docker-compose.example.yml up -d --build

# 停止并移除容器（不会删除 storage/）
docker compose -f docker_app/docker-compose.example.yml down
~~~

Compose 中的关键环境变量如下：

| 变量 | 默认示例 | 作用 |
| --- | --- | --- |
| <code>APP_REPO_ROOT</code> | <code>/workspace</code> | 容器内仓库根目录，供采集脚本和旧数据导入使用 |
| <code>APP_STORAGE_ROOT</code> | <code>/workspace/storage</code> | 应用运行时数据根目录 |
| <code>APP_THUMBNAIL_WORKERS</code> | <code>1</code> | 缩略图后台处理并发数，NAS 可按资源情况调整 |
| <code>APP_SECRET_KEY</code> | 未设置时使用内置开发默认值 | 可选的应用密钥；长期部署或对外开放时请设置自定义值 |
| <code>TZ</code> | <code>Asia/Shanghai</code> | 容器时区，影响任务时间和动态显示时间 |

### AMD64 NAS 使用预构建镜像

v12.8 的 AMD64 镜像包不纳入 Git 工作树。请从[RC-WebGallery Releases 页面](https://github.com/JDui/RC-WebGallery/releases)或其他正式发布交付物取得对应版本的 tar 包和 <code>.sha256</code> 校验文件；页面上的版本和资产名称以实际发布内容为准。将镜像包和对应部署包解压到 NAS，保留其中的目录关系；部署包已包含默认 Compose 和浏览器沙箱配置。在部署目录执行：

~~~bash
sha256sum -c zzs-bili-gallery_v12.8_amd64.tar.sha256
docker load -i zzs-bili-gallery_v12.8_amd64.tar
docker image inspect zzs-bili-gallery:12.8-amd64 --format '{{.Id}}'
~~~

启动前必须编辑 <code>docker_app/docker-compose.nas-amd64.yml</code> 的 <code>volumes</code>。文件中的示例宿主机路径是占位配置，请改成 NAS 上真实、可写的目录，并保留容器目标路径：

~~~yaml
volumes:
  - /your/nas/path/rc-webgallery/config:/storage/config
  - /your/nas/path/rc-webgallery/data:/storage/data
~~~

<code>/storage/config</code> 至少保存 <code>app.db</code>，<code>/storage/data</code> 保存图片、Live Photo、缩略图和其他运行时媒体。升级镜像时不要删除这两个宿主机目录。

确认路径后，从仓库根目录启动：

~~~bash
docker compose -f docker_app/docker-compose.nas-amd64.yml up -d
docker compose -f docker_app/docker-compose.nas-amd64.yml ps
docker compose -f docker_app/docker-compose.nas-amd64.yml logs -f --tail=200
~~~

NAS Compose 已将容器端口 <code>7860</code> 映射到宿主机 <code>7860</code>，访问地址为 <code>http://NAS地址:7860</code>。之后的停止、重启和升级命令：

~~~bash
# 停止并移除容器，保留宿主机数据卷
docker compose -f docker_app/docker-compose.nas-amd64.yml down

# 载入新 tar 后重建容器
docker load -i zzs-bili-gallery_v12.8_amd64.tar
docker compose -f docker_app/docker-compose.nas-amd64.yml up -d
~~~

NAS Compose 使用 <code>APP_REPO_ROOT=/workspace</code>、<code>APP_STORAGE_ROOT=/storage</code>、<code>APP_THUMBNAIL_WORKERS=1</code> 和 <code>TZ=Asia/Shanghai</code>。如果改动了环境变量或宿主机路径，重新执行 <code>up -d</code> 即可让 Compose 重建容器。

## 开发信息

### 目录与架构

~~~text
.
├── bili_9pics_downloader.py        # B 站九图动态采集
├── LivePhoto/                      # Live Photo 采集脚本与记录
├── docker_app/
│   ├── app/                        # FastAPI、SQLite、采集/同步/图库服务
│   ├── app/templates/              # Jinja2 页面模板
│   ├── app/static/                 # Alpine.js、原生 JavaScript 与 CSS
│   ├── rust/                       # indexer 与 media-worker Rust sidecar
│   ├── scripts/                    # 启动、占位数据和页面截图脚本
│   ├── tests/                      # Python 测试
│   ├── Dockerfile
│   └── docker-compose*.yml
└── storage/                        # 本地运行时数据，不纳入版本控制
~~~

应用后端使用 Python 3.12、FastAPI、Uvicorn、Jinja2、SQLite、APScheduler、Pillow、Requests、BeautifulSoup/lxml 和 qrcode；前端使用 Jinja2 模板、原生 JavaScript、CSS 及仓库内置的 Alpine.js；媒体探测、索引和部分后台工作由 <code>docker_app/rust/</code> 中的 Rust sidecar 辅助。

运行时目录约定为：

- <code>storage/config/app.db</code>：媒体、任务、审核、过滤日志与来源记录；升级后设置和订阅位于 <code>control.db</code>，浏览索引位于 <code>browse.db</code>。备份和恢复时保留整个配置目录。
- <code>storage/data/images/</code>：普通图片及其缩略图。
- <code>storage/data/livephoto/</code>：Live Photo 视频、封面和缩略图。
- <code>storage/data/</code> 下的其他目录：头像、缓存和应用维护数据。

大图库升级入口位于“设置 → 维护 → 数据检查”。数据库模块、媒体目录迁移、独占进度页、备份与中断恢复说明见 [数据结构升级](docs/data-layout-upgrade.md)。旧库不会自动移动媒体；升级前请保留同一时点的配置与媒体备份。

### 本地开发与测试

~~~bash
cd docker_app
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
PYTHONPATH=. APP_REPO_ROOT=.. APP_STORAGE_ROOT=../storage \
  uvicorn app.main:app --host 127.0.0.1 --port 7860
~~~

<code>requirements-dev.txt</code> 在运行时依赖之外加装 <code>pytest</code> 与 <code>httpx</code>；生产镜像只安装 <code>requirements.txt</code>。

在另一个终端运行测试：

~~~bash
cd docker_app
PYTHONPATH=. .venv/bin/pytest -q
~~~

页面审查截图使用隔离占位数据。<code>review_capture.sh</code> 当前针对 macOS 的 Lima Docker 流程：需要本机已有可运行的 <code>limactl</code>、Lima 的 <code>lima-docker</code> context、Docker、Playwright 浏览器和审查镜像；脚本会在 <code>docker_app/review/&lt;时间戳&gt;/</code> 下生成临时运行目录与截图：

~~~bash
bash docker_app/scripts/review_capture.sh zzs-bili-gallery:12.8-amd64
~~~

脚本内部调用 <code>seed_review_data.py</code>，不会读取真实 <code>storage/</code>。如果只需要生成占位数据库和媒体，可单独执行：

~~~bash
docker_app/.venv/bin/python docker_app/scripts/seed_review_data.py \
  --storage-root /tmp/rc-webgallery-review
~~~

当前应用版本由 <code>docker_app/app/version.py</code> 中的 <code>APP_VERSION = "12.8"</code> 定义；对应 NAS 镜像标签为 <code>zzs-bili-gallery:12.8-amd64</code>，镜像包文件名为 <code>zzs-bili-gallery_v12.8_amd64.tar</code>。

## 小红书个人点赞

小红书浏览器内置于主应用，首次在真实页面手动扫码，后续复用私有浏览器配置目录。主应用不读取或导出 Cookie。此功能只归档本人点赞，不支持他人点赞、发布或自动点赞；登录失效、验证码、平台限制和网页结构异常会停止任务，等待人工处理。页面适配尚需使用者在自己的账号环境验证，不能将合成测试当作真实账号验收。

“启动扫码登录”默认在订阅卡片内显示二维码，过期会自动刷新；扫码确认后应用检测到登录即自动完成收尾。检测到验证码时自动展开同一 Chromium 会话的验证画面，优先截取验证码区域，无法定位时显示完整页面；也可随时点击“打开验证画面”或在画面中切换“显示完整页面”。支持人工点选、拖动、文字输入、退格与回车，拖动轨迹在松开后按操作时序回传。验证画面通过普通 HTTP 请求更新，不依赖 noVNC 脚本、WebSocket 或额外登录端口；跨域验证码仍留在原浏览器中运行，不复制第三方验证模块。操作说明收纳在“？”中，失败和过期会直接显示提示。小红书返回 IP 风险等安全限制时会展示实际页面和限制提示，这类限制需要按平台提示处理，显示画面不能代替平台放行。

### 开箱使用

v12.8 主镜像内置浏览器。按前文使用默认 NAS Compose 启动即可，整个应用只有一个容器和网页端口 `7860`；不需要另外部署浏览器服务、创建密钥、映射登录端口或设置 SSH 转发。源码部署也使用默认的 `docker-compose.example.yml`。

升级时导入新镜像，使用本版本部署包中的 `docker_app/docker-compose.nas-amd64.yml`，保留原来的两条配置、数据挂载路径，然后正常重建主容器。部署包中的 `docker_app/xhs_worker/seccomp_profile.json` 与 Compose 保持相对位置；默认配置会自动使用它启用 Chromium 沙箱。

首次启动自动在持久化配置目录下初始化 `xhs-private/`，保存浏览器 Profile、账号摘要盐和内部通信密钥。此目录及其密钥保持私有权限，整个配置目录不对 HTTP 静态文件接口开放。升级和重建时保留原配置挂载，登录状态会继续复用；备份时备份整个配置目录，恢复浏览器数据前先停止容器。浏览器进程以非 root 身份运行；主服务负责自动启动、异常重启和退出清理，浏览器及内部通信端口仅监听容器回环地址。Chromium 沙箱依赖 NAS 内核支持，出现异常时页面显示错误，不会自动关闭沙箱继续运行。沙箱配置基于 [Playwright 官方 Docker 指引](https://playwright.dev/python/docs/docker)。

### 旧容器升级后的浏览器启动检查

仅替换镜像会保留旧容器的安全限制。若启动扫码时报“容器配置阻止了浏览器沙箱”，在部署包目录保留原有配置、数据挂载路径，使用随包默认配置重建一次主容器：

```bash
docker compose -f docker_app/docker-compose.nas-amd64.yml up -d --force-recreate
```

随包的 `docker_app/xhs_worker/seccomp_profile.json` 与 Compose 保持相对位置即可，无需单独启动服务。二维码取自内置 Chromium 打开的官方登录页，完整登录窗口显示的也是容器内画面，不需要本机另装浏览器。启动失败时，小红书 tab 会显示脱敏原因与处理建议；不会自动关闭 Chromium 沙箱。

### 媒体归档与视频播放

本人点赞的笔记默认全部允许归档，不使用 B 站或普通站点的关键词、白名单和自动审核规则。旧版被这类规则拦截的笔记会重新排队；用户主动删除、屏蔽或移入垃圾桶的内容继续保留该操作。

笔记媒体从官方页面的完整详情数据读取，不采集正文中的表情、头像或列表占位图。无法确认完整图片清单或视频地址时保留为待解析，不标记为已归档。升级后，旧版浏览器来源已归档的笔记会重新进入待修复队列；点击该来源的“重试媒体”，可重新读取并替换旧的错误归档。

每个小红书订阅提供“完全重置抓取”按钮，连续三次确认后清除此来源的媒体、收藏、扫描与任务历史，保留登录与来源设置，恢复首次拉取状态并重新开始；有执行中或排队任务时须先停止。

小红书贴文的删除操作先清理本地内容，并在后台尝试取消原帖点赞；取消失败不会恢复本地内容，结果写入任务记录。其他来源的垃圾桶操作不变。

“查看最近 50 条笔记”的标题打开应用内归档；尚未归档时会提示重试媒体。小红书视频笔记使用独立播放浮窗，打开即尝试静音播放，可使用原生播放控件开启声音；关闭浮窗会停止播放。未知发布日期显示未知，不使用 1970 年占位日期。

### 首次登录和订阅

1. 打开“订阅管理 → 小红书 → 我的点赞”，点击“启动手动登录”。
2. 页面会直接显示登录二维码，用小红书 App 扫码并在手机上确认；二维码过期自动刷新，检测到登录成功后自动完成收尾，也可随时点“完成扫码”。拿不到二维码或需要过验证码时，点“打开完整登录窗口”人工操作，弹窗被拦截时可用“在当前页面打开”。无需输入远程桌面密码；登录会话十分钟后自动关闭。完整登录窗口的画面使用镜像内打包的单个脚本，不依赖外部 CDN；出现组件加载错误时会显示请求状态，HTTP 503 表示镜像缺少组件，需要更新完整镜像。
3. 登录后账号标识自动填入创建表单；填写首次拉取条数、锚点数和独立刷新周期，创建来源。
4. 在该来源中确认本人账号、点赞页可翻页及访问规则。只有实际确认排序稳定时才勾选“启用锚点早停”；未确认时使用保守完整扫描。点击“验证本人点赞源”后可立即同步，也可单独开启自动刷新。

小红书不继承站点代理、系统环境代理或 B 站刷新周期。基础周期至少 23 分钟，每次任务完成后重新抽取提前或延后 1–22 分钟的偏移。首次拉取条数始终可修改；变更后下次同步按新上限重新扫描，跳过旧锚点并复用已下载文件。首次读取达到条数上限时记为部分扫描；页面完整性可靠时会保存本轮前部笔记为锚点，已确认排序稳定的来源下轮即可使用锚点早停，不要求先读完整个点赞列表。定期完整核对从首次限量基线或最近一次完整扫描起算，普通增量命中不会推迟核对。旧版已标记首次完成但没有检查点的来源，会在下次同步按首次条数重建锚点。首次限量扫描和正常锚点早停均不据此判断旧点赞已取消。增量扫描翻过 2 倍首次拉取条数（设为“不限”时按 1000 条计）仍未命中任何锚点时按止损结束，本轮前部笔记提交为新锚点，下一轮即可恢复早停；止损轮不算完整快照，同样不取消旧点赞。扑空轮在读过的笔记中发现与锚点同作者且标题吻合的笔记（疑似作者删旧发新、更换了笔记 ID）时视同触底：超过首次拉取条数后即提前结束并按本轮前部刷新锚点；标题比对仅作止损信号，不参与锚点 ID 命中判定。仅可信自然末页会更新取消点赞状态，保留本地归档和收藏。还可使用“忽略锚点完整扫描”进行核对。

当前版本优先尝试小红书提供的原始视频 Key 与可用的 screencast 资源，再选择其他视频流，并将疑似带水印的流降为回退选项。原始流只做短时探测，失败或超时后自动回退到可播放流，单个媒体项受总时长预算约束，不会拖住整次同步；媒体失败会保留具体原因（如来源不可用、类型不符、超时或解码失败），不再统一显示为浏览器服务错误。是否能取得无水印版本取决于笔记实际提供的资源，仍需使用真实笔记样本验证。未知资源先保留解析待办；可用“重试媒体”重新解析过期地址，已下载成功的文件不重复下载。“检查本地归档”离线比对下载缓存与图库文件的 SHA-256，将缺失或损坏项保留为待重试，不访问小红书。图文、视频封面与明确存在的 Live Photo 分别归档；普通视频不计为 Live Photo。实况视频按小红书网页端的真实结构探测：优先读取图片条目自带的 `stream`（各编码的 `masterUrl` 与 `backupUrls`），兼容把视频放在 `live_photo` 子对象里的旧形态，并与视频笔记共用同一套无水印排序、原始流短探测与备用地址回退预算。SSR 快照里看不到实况流时会再多等几次 `/v1/feed` 响应，避免实况被静默丢掉；实况取不到只记该条失败并保留静态图，不会连带整篇笔记归档失败，解析诊断会区分“载荷里确实有实况线索”与“本来就没有实况”。本人点赞笔记不经过普通站点的关键词过滤；手动排除仍生效。

Live Photo 以同序号的静态图和 MP4 视频分别保存，并在图库中配对播放。载荷声明实况但缺少视频地址，或视频下载失败时，静态图仍可归档，笔记显示“静态图已归档，实况待重试”，不会误记为全部完成；“重试媒体”会重新解析并补齐实况，复用已经下载成功的文件。升级后，旧版浏览器来源中已完成的图文笔记会一次性重新检查实况，保留本地文件，无须完全重置抓取。格式与验证说明见 [小红书 Live Photo](docs/xhs-livephoto.md)。

任务统一进入任务中心，支持暂停、继续、取消、重试并遵循前台优先级。主应用重启会将未结束的小红书任务标记为可重试，清理孤立 `.part` 文件；已提交笔记、待办与读取锚点保留。注销登录态需要确认，只清除私有浏览器 Profile 并停用对应来源，既有图库文件不删除。

也可直接导入净化的 [Manifest v1 示例](docs/examples/xhs-manifest.example.json)。清单最多 4 MiB，不能包含 Cookie、认证头、签名 URL 或 `xsec_token`；手工清单不会改变扫描锚点或推断取消点赞。

## 内网自动跳转

在“设置 → 网络”中可分别配置“采集代理”和“内网访问”。取消“共用代理 IP”时，“代理 IP”和“内网 IP”是两个独立输入框，各自保存；勾选后内网使用代理 IP，原先的独立内网 IP 仍会保留。内网访问支持开关自动跳转及设置服务端口和协议，点击“保存”后写入同一份 `config.json`。默认关闭。也支持直接编辑配置文件；设置页重新打开后读取文件最新值。

当前网页通过 RFC1918 IPv4 或 IPv6 ULA 地址访问时，页面四周显示彩虹边缘；该提示与自动跳转开关独立，不代表代理状态。域名访问和回环地址不显示。边缘使用静态渐变，不增加持续动画开销。

应用启动时会在持久化配置目录创建 `config.json`。源码部署路径为 `storage/config/config.json`；NAS Compose 中为容器内的 `/storage/config/config.json`（对应你挂载的宿主机配置目录）。可参考 [配置示例](docker_app/config.example.json)：

```json
{
  "intranet_auto_redirect_enabled": false,
  "intranet_use_proxy_host": false,
  "intranet_redirect_host": "192.168.31.221",
  "intranet_redirect_port": 7777,
  "intranet_redirect_protocol": "http"
}
```

- `intranet_auto_redirect_enabled`：改为 `true` 开启，改回 `false` 关闭。配置文件在每次打开或刷新页面时重新读取，不需要重启；已经打开的页面不会被强制跳转。
- `intranet_use_proxy_host`：为 `true` 时共用设置页“代理 IP”的值，忽略 `intranet_redirect_host`。只共用 IP；仍使用 `intranet_redirect_port`，不会使用代理端口，也不要求启用站点采集代理。
- `intranet_redirect_host`：独立设置目标 NAS 的内网 IP；支持 RFC1918 IPv4 和 IPv6 ULA。不能填写带协议、路径或端口的 URL，不能填写容器回环地址 `127.0.0.1`。
- `intranet_redirect_port` 与 `intranet_redirect_protocol`：填写内网访问时实际使用的服务端口和 `http` / `https`。

从其他地址访问时，浏览器先用无 Cookie 的健康探测确认目标是本应用，成功后保留路径、查询参数和锚点切换地址。已在目标内网地址时不会重复跳转。无法连接、服务身份不匹配或超过 1.5 秒时继续当前地址；配置缺失、格式无效或关闭时直接正常加载。仅健康探测接口允许跨域，不扩大业务 API 的跨域权限。

浏览器可能限制从 HTTPS 页面探测 HTTP 内网或要求本地网络权限；被阻止时会继续使用原地址，不绕过浏览器保护。该功能不会修改系统代理、Clash 规则或 NAS 采集代理。不同访问地址的浏览器本地偏好独立保存，当前搜索、视图等未编码进 URL 的状态不会跨地址迁移。

## 页面体验修正

- 顶部明确区分动态总数与 Live Photo；搜索命中数独立于当前图库，结果页改用紧凑布局，并提供“最近入库”排序和未来来源日期提示，保留原始日期。
- 图库与图片入口可使用键盘操作，详情和查看器提供对话框语义、焦点约束及返回焦点；查看器移除顶部标题条，序号和图标翻页集中到右下角的半透明工具栏，删除与其他按钮保持同高对齐。
- 任务徽标表示运行及等待任务，历史列表使用中文状态和可读时间；垃圾桶明确说明文件清理、解除拦截与重新拉取的关系，并支持搜索记录。
- 设置按用途分组，显示未保存状态、保存反馈与字段校验；改善辅助信息对比度、完整标题提示、订阅日期名称和网格列数说明。
- 搜索、垃圾桶、订阅及设置中的文本、日期、数字和选择控件共用 `ui-field` 样式，统一边框、圆角、聚焦和禁用状态；桌面侧栏宽度缩为 236px，折叠为 72px，手机抽屉上限为 280px。桌面按钮默认 36px、分段选项 30px，取消固定按钮宽度；说明收纳到悬停提示，支持键盘聚焦、Escape 关闭及触屏点击。设置分组与保存区合并，任务、空状态和订阅卡片使用紧凑间距。

前端状态及跳转分支回归可运行 `node --test docker_app/tests/ui_state.test.cjs docker_app/tests/ui_help.test.cjs docker_app/tests/progressive_images.test.cjs`；Python 回归在 `docker_app` 目录运行 `python3 -m pytest -q`。

界面与内网检测页的圆角统一使用 `docker_app/app/static/ui-tokens.css` 中的令牌：按钮和表单控件 10px、卡片 12px、外框及弹窗 18px、内嵌缩略图和标签 8px。组合按钮外框按内层圆角加内边距计算；开关、进度轨道和圆形状态点保留功能形状。

图库与动态详情共用单档 thumb 缩略图，默认短边 368px、WebP 质量 50；存储及配置兼容字段沿用 `.thumbs/small`、`small_thumb_edge` 和 `small_thumb_quality`。查看器再加载原图。升级自动迁移原有默认值，保留自定义参数，并通过页面索引版本更新补齐缩略图。旧 thumb 与 tiny 媒体派生图纳入垃圾文件统计和清理；头像、原图和仍作为有效封面的文件保留。

“设置 → 界面”支持宽屏切换单张简易封面，默认仍为多图聚合；单张封面的随机选图默认开启，两项均保存在服务端。随机候选从整组媒体抽取并随索引保存，避免缓存刷新时封面跳动。动态详情下滑后保留紧凑顶部栏，提供标题、上一篇、下一篇和关闭入口。

图片按视口流式加载，全局最多并发 6 个预加载与解码任务；离屏卡片释放图片源，移除视频节点时停止播放并释放资源。卡片刷新通过稳定 key 原位合并，已显示图片再次出现时不重复淡入；本地图库页面缓存最多保留 12 份，质量记录有上限。全屏查看器的彩虹加载层使用淡入淡出，并遵循系统减少动画偏好。

相关回归检查：`node --test docker_app/checks/gallery_loading.test.cjs docker_app/checks/image_resources.test.cjs`，以及在 `docker_app` 目录执行 `PYTHONPATH=. python -m pytest -q checks/test_thumbnail_policy.py`。

图库卡片显示内容作者：小红书使用笔记作者用户名，B 站使用 UP 主名称，站点使用站点名。贴文详情的时间后显示“ - 作者名”，作者文字可直接打开对应主页；订阅分组仍使用原订阅名。历史小红书记录未保存用户名时先显示作者 ID，后续扫描或重试媒体会补齐用户名。视频卡片悬停时保留彩色边框。

排序通过“随机 / 按序”药丸切换；按序依次提供“最近入库、最新发布、最早发布”，切回按序时保留本次选择。设置中的“默认随机”开关决定打开或重新进入全部项目时是否使用随机排序，关闭时初始使用最近入库。

随机排序使用连续瀑布流，与其他排序一样支持自动或手动加载。浏览器缓存本轮随机种子，普通刷新、重新打开页面和切换排序均沿用原种子；只有窄屏或方屏在顶部通过触摸或触控板下拉刷新，或桌面端（方屏或宽屏）点击药丸旁的“手气不错”，才生成新一轮随机顺序，新入库内容随新一轮参与排序。随机顺序缓存在 SQLite 中，读取后续页面无需重新抽取或遍历深分页；最多保留 16 轮，过期后页面提供“重新开始浏览”。窄屏到方屏的图库按内容可用宽度自动适配每行 2～4 张卡片，宽屏保留密度设置。

图片查看器支持上下滑动切换图片，右滑返回、左滑展开右侧居中的竖向工具栏；鼠标靠近右侧时滑出，离开触发区域 1 秒后收回。工具栏按删除、留空、上一张、张数/总数、下一张、留空、关闭排列。方向键上/下切图、方向键右返回、方向键左展开工具栏；双击或双指捏合缩放，放大后拖动或触控板滚动平移。窄屏和方屏支持触控板手势：横向右滑返回或打开菜单、上下切换视频、随机列表顶部下拉刷新。每次触控板手势只触发一次操作，惯性滚动不会连续穿透多层页面。
