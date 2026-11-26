# AI_Relay_Station
用来存储中转站的代码

## 家庭办公聊天

基于 Open WebUI 0.13.5 和 Python 3.12，使用独立 systemd 服务及 SQLite。浏览器打开部署实例的 HTTPS 地址，通过账号登录。公开注册已关闭。

## 第一次使用

1. 打开左下角头像 → 设置 → 管理员 → 外部连接，编辑 OpenAI 兼容连接。国内 Kimi 的 Base URL 为 `https://api.moonshot.cn/v1`。在 HTTPS 管理页面填写本人服务商密钥，保存并刷新模型列表。
2. 新建聊天，选择支持图片输入的模型。在工作区的模型设置中开启 Vision 和文件上传。用输入框旁的加号选择图片、Excel 或 Word 后提出问题。
3. 在实例中打开 `/office-balance`。当前支持 DeepSeek 官方接口和 Kimi 国内官方接口；其他自定义连接会显示支持状态。

## 附件

图片作为视觉输入。Office、PDF 和文本附件由独立的限时进程提取正文。每个文件上限 8 MiB，解析后最多 8 万字符，每次最多 3 个文件。

| 类型 | 格式与处理范围 |
| --- | --- |
| 文档 | DOC、DOCX、RTF、ODT；提取正文，DOCX 含表格文本 |
| 表格 | XLS、XLSX、XLSM、ODS、CSV、TSV；Excel 提取各工作表和已保存的公式结果 |
| 演示 | PPT、PPTX、PPTM、ODP；提取幻灯片文字 |
| PDF | 提取文字层；纯扫描件可先 OCR，或将页面作为图片发送 |
| 文本与数据 | TXT、Markdown、HTML、JSON/JSONL、XML、YAML、TOML、INI、日志、SQL 和常见代码文件 |

支持带 BOM 的 UTF-16、UTF-8 及常见 GBK/GB18030 中文文本。旧版 Office 建议使用未加密的文件。聊天消息和附件可保留在实例中，提问会把相关内容发往选定的服务商。

## 个人 API 和模型

登录后进入「设置 → 个人 → 管理连接」，点击加号即可在原生连接窗口配置模型。旧书签 `/personal-models` 会打开同一设置。每个账号可分别管理多个 OpenAI Chat Completions 兼容模型。填入显示名称、公网 HTTPS Base URL、服务商的模型 ID 和本人的 API Key，保存后可直接在聊天的模型列表中选择。密钥加密保存在数据目录下的 personal-models.sqlite3，备份时需同时保存 WEBUI_SECRET_KEY。更换地址需重新输入密钥。

个人模型列表、修改、删除及推理按当前登录用户鉴权。管理员明确共享的基础模型沿用站点授权配置。Open WebUI 0.13.5 的聊天模型集成使用 deploy/install_personal.py 安装，升级版本前须核对补丁与账号隔离测试。

个人和管理员共用 Open WebUI 的连接设置组件。服务端补丁装入虚拟环境后，前端需使用同版本的源码构建。在装有 Node.js 22 的构建机上，下载并解压 PyPI 发布的 open-webui 0.13.5 源码包，运行 `sh deploy/build_frontend.sh /path/to/open_webui-0.13.5`。把生成的 `build/_app/immutable/` 合并至 wheel 的 `open_webui/frontend/_app/immutable/`，保留旧的哈希资源供已打开的聊天使用。备份后替换该目录下对应的 `_app/version.json` 和 `index.html`，最后重启服务。

## 部署与运维

应用目录为 `/opt/office-webui`，数据目录为 `/var/lib/office-webui`。备份应同时保留应用数据和仅 root 可读的 `/etc/office-webui.env`。TLS 使用服务器已有的 IP 证书，Nginx 在 8443 接受访问并转发到回环地址 18765。

安装采用该版本官方发布包中 `backend/requirements-slim.lock` 的 Linux CPython 3.12 依赖与预构建 Open WebUI wheel。使用虚拟环境按 slim 锁文件安装后，单独以 `--no-deps` 安装 `open-webui==0.13.5`，再安装 `openpyxl==3.1.5` 和 `defusedxml==0.7.1`。安装配置见 deploy/。启用 Nginx 配置前，将模板中的 relay.example.com 替换为实际证书名和站点地址。首次启动需要在环境文件中设置随机的 WEBUI_SECRET_KEY、EXTERNAL_DOCUMENT_LOADER_API_KEY，以及 WEBUI_ADMIN_EMAIL、WEBUI_ADMIN_PASSWORD。管理员创建后可移除后两项。部署时将 CORS_ALLOW_ORIGIN 与 WEBUI_URL 设为实例的 HTTPS 地址。

`systemctl status office-webui` 查看状态；`systemctl restart office-webui` 重启。更新主版本时重新验证附件解析、登录鉴权和扩展路由。pytest 覆盖 Office 解析、大小限制、鉴权及余额服务商契约。

## 办公站出站转发

模型列表、推理请求和余额查询均使用办公服务账户的独立策略路由。Mihomo 提供 office-tun，规则表 21875 对应该账户，隧道不可用时其外部连接会失败。回环上的 Nginx 和文档解析继续走本地。其他服务使用原有路由。入口、数据库及业务 API 保持现有配置。

部署模板在 deploy/egress/。实例的控制接口使用权限受限的 Unix socket，订阅地址在服务器 /etc/office-egress/sources（主、备两行），节点在 /var/lib/office-egress/nodes.yaml。两者按 0600 管理。仅引入订阅的节点参数，路由、TUN 和控制接口由项目配置。健康节点由 fallback 组选择。应用和转发器由 systemd 管理。

服务器每日依次尝试主订阅和备用订阅。刷新失败保留最近可用版本；切换时网络探测失败会恢复旧节点。检查定时器可用 `systemctl list-timers office-egress-refresh.timer`。如遇订阅源 IP 鉴权，可从已获订阅授权、可 SSH 登录服务器的电脑运行 `python3 deploy/egress/sync_local.py`（需要 httpx，可通过 OFFICE_SSH_TARGET 或本地 .private/ssh-target 指定 SSH 目标）。脚本从服务器读取地址，在本地获取订阅，经 SSH 同步，密钥不进入命令行或项目文件。

文档扩展依赖见 deploy/requirements.in。Ubuntu 的旧版 DOC/PPT 读取命令来自 catdoc 包。将 document_formats.py 与 extractor.py 同目录部署；解析器使用单独的进程与内存、CPU 限制。
