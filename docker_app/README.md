# RC-WebGallery｜Docker 部署说明 v12.0

RC-WebGallery 是面向个人收藏与自托管场景的多来源媒体归档图库。当前来源包括 B 站动态与 Live Photo、小红书本人点赞笔记，以及 RSS、Sitemap 或 HTML 列表页形式的通用站点；不同来源共用任务中心、索引、筛选和图库界面。

## 启动

1. 在仓库根目录执行 `bash docker_app/scripts/bootstrap_macos.sh`
2. 然后执行 `docker compose -f docker_app/docker-compose.example.yml up --build`
3. 打开 `http://localhost:7860`

## NAS 部署

AMD64 NAS 可以直接使用 [docker-compose.nas-amd64.yml](docker-compose.nas-amd64.yml)。

建议流程：

1. 导入镜像包：`docker load -i zzs-bili-gallery_v12.0_amd64.tar`，并解压同版本部署包，保留其中目录关系。
2. 在默认 Compose 中保留你原有的两个配置、数据挂载路径；升级时使用随包的新版本 Compose。
3. 在部署包根目录执行 `docker compose -f docker_app/docker-compose.nas-amd64.yml up -d`。

小红书浏览器会随主服务自动启动，直接在网页中扫码使用。主应用通过 `7860` 网页端口提供界面；当前 v12.0 发布包的镜像 tar 与镜像标签仍沿用历史名称 `zzs-bili-gallery`，请按对应 Release 资产的实际名称加载，不要据此判断仓库项目名。

默认挂载说明：

- `/storage/config` 对应宿主 `config`
- `/storage/data` 对应宿主 `data`

## 审查截图

在本机关闭 Docker 前，可以先生成关键页面截图：

1. 确保已经构建好镜像，例如 `zzs-bili-gallery:amd64`
2. 执行 `bash docker_app/scripts/review_capture.sh zzs-bili-gallery:amd64`
3. 截图会输出到 `docker_app/review/<时间戳>/screens`

## 功能

- B 站九图动态与 Live Photo 拉取
- 小红书本人点赞笔记的图文、视频与 Live Photo 归档
- RSS、Sitemap 与 HTML 列表页形式的通用站点订阅
- 二维码扫码登录与 Cookie 检查
- 推广过滤与待审核列表
- 相似图片重复内容检测与贴文集中处理
- 缩略图 WebP 预生成
- Live Photo 预览、循环、乒乓、单次、不播放
- 原始媒体删除后的缩略图销毁与索引清理
- 前台加载优先调度，后台拉取、删除、图标刷新和重复检测会主动让行

## 界面与动效

界面使用浅色液态玻璃风格，统一导航、图库、审核、任务、订阅、设置及详情浮层的材质、圆角和光影。导航使用内置 SVG 图标，选项控件带滑动指示块，页面切换、按钮按压与筛选展开都有轻量动效；桌面导航和顶栏支持随鼠标移动的高光。

在「设置」中选择低功耗模式会关闭背景模糊和鼠标高光，标签切换保留短暂的淡入。系统开启「减少动态效果」时，界面会减少过渡并关闭页面切换动效。浏览器不支持背景模糊时使用清晰的实色面板。玻璃效果不会改变原图与视频的颜色。

视觉样式与动效分别维护在 `app/static/liquid-glass.css` 和 `app/static/liquid-glass.js`。前端资源使用独立的样式修订号刷新缓存；升级现有 Docker 部署需要重新构建镜像。

## 小红书

主镜像已内置小红书浏览器，使用默认 Compose 自动初始化，无需额外容器、密钥脚本、登录端口或 SSH 转发。打开页面即可启动扫码窗口；首次扫码与本人点赞源验证后启用同步。固定直连，独立周期逐轮随机前后抖动 1–22 分钟。使用与归档恢复说明见[项目 README](../README.md#小红书个人点赞)。一次性测试脚本、原始日志、截图和设计过程记录统一放在仓库 `temp/`。

## 任务优先级

- 前台任务：页面、瀑布流、详情、缩略图及媒体内容加载
- 后台任务：拉取、删除与文件清理、图标获取、重复检测、索引和缩略图重建
- 前后台同时发生时，后台任务会在处理步骤之间等待前台请求完成；缩略图后台并发默认限制为 1

## 数据目录

- `storage/config/app.db`
- `storage/data/images`
- `storage/data/livephoto`
