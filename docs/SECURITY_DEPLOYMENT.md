# 管理员登录与 HTTPS 部署

首次启动需要管理员密码哈希，用于初始化用户数据库。数据库已有用户后，可以不再提供
初始化哈希。不要把明文密码放入配置。

默认通过 HTTP 访问，登录使用 HttpOnly、SameSite=Strict 会话 Cookie。
使用 `uv` 启动时保留原来的业务参数，运行示例：

```bash
export PATCHKIT_ADMIN_PASSWORD_HASH_FILE="$PWD/.secrets/admin-password.hash"
export PATCHKIT_COOKIE_SECURE=false
uv run --no-sync wsi-patchkit-viewer --host 0.0.0.0 --port 8000 --slide-dir ./data/slides
```

浏览器访问 `http://192.168.0.133:8000`。启用 HTTPS 时使用下面的证书或代理配置，
并设置 `PATCHKIT_COOKIE_SECURE=true`。

## 1. 生成管理员密码哈希

在项目目录执行：

```bash
uv sync --extra web
mkdir -p .secrets data/slides data/overlays data/crops data/uploads
chmod 700 .secrets
uv run patchkit-password-hash > .secrets/admin-password.hash
```

程序会在终端提示输入并确认密码（至少 12 个字符），文件只保存随机盐和
PBKDF2-SHA256 哈希（600,000 次迭代）。命令失败时不要继续部署，重新生成哈希。
`.secrets` 和 `.env` 已从 Git 和 Docker 构建上下文排除。容器 UID 10001 必须能读取
哈希文件；可让文件保持 644、父目录保持 700，Docker 将单独挂载该文件。

账号默认 `admin`。复制 `.env.example` 为 `.env`，填写实际域名和目录。
确保裁剪和上传目录可由 UID 10001 写入，例如在专用数据目录执行：

```bash
sudo chown 10001:10001 data/crops data/uploads
```

## 2. 公网域名 HTTPS

要求 Docker Compose 2.24.4 或更高版本。将域名的 DNS 指向部署机器，开放 TCP
80/443；UDP 443 用于可选的 HTTP/3。`.env` 中的 `PATCHKIT_DOMAIN` 只填域名，
例如 `slides.example.com`，不带协议或路径。错误的 AAAA 记录也会影响证书签发。

```bash
docker compose -f compose.yaml -f compose.https.yaml config --quiet
docker compose -f compose.yaml -f compose.https.yaml up -d --build
```

访问 `https://你的域名`，首次进入显示管理员登录页。Caddy 自动申请、续期证书并
将 HTTP 重定向至 HTTPS。证书保存在 `caddy-data` 卷中；不要随意删除该卷。
后端不发布宿主机端口。只有专用网络中地址为 `172.30.87.2` 的代理受到 Uvicorn
信任，用于识别 HTTPS 并生成正确的 IIIF 地址。

如果该子网与现有网络冲突，同时修改 `compose.https.yaml` 的子网、Caddy 地址
和 `FORWARDED_ALLOW_IPS`。不要把可信代理配置成 `*`。本方案中代理到后端的 HTTP
位于同一宿主机的 Docker 网络；跨主机部署应为这一段另外配置 TLS。

检查状态：

```bash
docker compose -f compose.yaml -f compose.https.yaml ps
docker compose -f compose.yaml -f compose.https.yaml logs --tail=80 caddy
curl -I https://你的域名/
curl -i https://你的域名/api/slides
```

预期首页跳转 `/login`，未登录 API 返回 `401`。登录后打开切片、上传、裁剪、下载、
删除，退出后再次请求数据应返回 `401`。`/healthz` 公开且只返回存活状态。

## 3. 内网 HTTPS

### uv 直接启动（无需反向代理）

启动命令支持成对指定 `--ssl-certfile` 和 `--ssl-keyfile`。例如服务器 IP 为
`192.168.0.133`，先在项目目录生成证书：

```bash
bash deploy/generate-local-tls.sh 192.168.0.133
```

在每台浏览器客户端安装 `.secrets/tls/root-ca.crt` 到“受信任的根证书颁发机构”。
只复制这个公开证书，两个 `.key` 文件始终留在服务器。Windows 可双击证书进入
安装向导；浏览器若使用独立信任库，也需要在其证书设置中导入根 CA。
确认这是自己服务器生成的 CA 后再安装。

停止原来的 HTTP 进程，保留原来的切片、上传、裁剪等参数，增加 TLS 参数：

```bash
export PATCHKIT_ADMIN_PASSWORD_HASH_FILE="$PWD/.secrets/admin-password.hash"
export PATCHKIT_COOKIE_SECURE=true
uv run --no-sync wsi-patchkit-viewer \
  --host 0.0.0.0 --port 8000 \
  --slide-dir ./data/slides \
  --ssl-certfile .secrets/tls/server.crt \
  --ssl-keyfile .secrets/tls/server.key
```

访问 `https://192.168.0.133:8000`。该端口只接受 TLS，HTTP 无法连接；直接启动方式
不会在同一个端口提供 HTTP 跳转。若需要 HTTP 自动跳转，使用 Caddy 部署。
启用 Secure Cookie 模式后，应用也会拒绝非 HTTPS 业务请求（健康检查除外）。

服务端证书含有指定 IP 的 SAN，有效期一年，CA 有效期十年。服务器 IP 改变或证书
即将过期时，选择新的输出目录重新运行脚本；需要沿用客户端已信任的 CA 时，把旧的
`root-ca.crt` 和 `root-ca.key` 安全复制到新目录后生成。脚本拒绝覆盖已有服务端证书。

验证证书和服务：

```bash
openssl verify -CAfile .secrets/tls/root-ca.crt -verify_ip 192.168.0.133 .secrets/tls/server.crt
curl --cacert .secrets/tls/root-ca.crt https://192.168.0.133:8000/healthz
```

“无法提供安全连接”通常表示访问了仍在提供 HTTP 的端口；证书不受信任则需要安装
根 CA，域名或 IP 不匹配则需要重新签发匹配访问地址的证书。只把 URL 改成 HTTPS
不会让原来的 HTTP 服务启用 TLS。

### Caddy 内网部署

没有公网域名时，可使用组织颁发的证书，或将 Caddy 站点改为内网域名并增加
`tls internal`。使用内置 CA 时，每台客户端都必须安装并信任其根证书。
不要通过关闭浏览器证书校验来部署。站点可暂不使用 HSTS，直到客户端信任已配置好。

## 4. HTTP 访问

保留登录，通过 HTTP 访问：

```bash
export PATCHKIT_ADMIN_PASSWORD_HASH_FILE="$PWD/.secrets/admin-password.hash"
export PATCHKIT_COOKIE_SECURE=false
uv run wsi-patchkit-viewer --slide-dir ./data/slides
```

访问 `http://127.0.0.1:8000`。基础 `docker compose up --build -d` 默认只绑定
`127.0.0.1:8081`，默认设置为 `PATCHKIT_COOKIE_SECURE=false`。
HTTPS Compose 会强制恢复 Secure Cookie。

完全关闭认证只限本机开发，可显式运行：

```bash
uv run wsi-patchkit-viewer --no-auth --slide-dir ./data/slides
```

## 会话与接口行为

- 会话默认有效 8 小时，固定过期；`PATCHKIT_SESSION_SECONDS` 可设为 1–604800 秒。
- Cookie 只携带随机会话 ID，设置 HttpOnly、SameSite=Strict；HTTPS 配置下额外设置
  Secure 并使用 `__Host-` 前缀。服务端保存会话 ID 的 SHA-256 摘要及独立 CSRF token。
- 上传、裁剪、删除、退出等操作要求同源 `Origin` 和 `X-CSRF-Token`。
  登录也要求同源 Origin。脚本客户端需先登录，再从 `/api/auth/session` 取得 token。
- 所有业务接口、IIIF、图片、下载、静态业务脚本和 API 文档统一校验会话。
  仅登录页及其资源、登录接口、健康检查公开。
- 每账号每分钟最多 10 次登录或密码验证尝试，每来源 IP 每分钟最多 60 次，超出返回
  `429`。限流计数也在 SQLite 保存，同一数据库的服务实例共享计数。
- 每账号最多保留 32 个会话，超出时撤销最早创建的会话；其他账号不受影响。
  再次登录会轮换当前浏览器中该账号的会话。
- 用户和会话默认保存到 `./data/auth.sqlite3`。通过 `PATCHKIT_AUTH_DB` 指定位置，
  重启仍可使用未过期的会话。多个实例可共享同一台机器本地磁盘上的数据库；不把
  SQLite WAL 数据库放到 NFS 等网络文件系统，多机部署应改用集中式数据库。
- 退出立即撤销当前会话；退出全部设备、修改密码和停用账号会立即撤销该用户全部会话。
- 认证模式下响应设置 `Cache-Control: no-store`，避免 HTTP 缓存保留受保护图像。
  已下载文件和已显示在浏览器中的图像无法远程撤回。

## 用户数据库和账号接口

数据库自动创建，使用当前普通用户权限运行即可，无需额外数据库服务或 Python 依赖。
文件包含用户密码哈希、会话摘要和 CSRF token，需要与业务数据一起保护并备份。
备份时先停服，或使用 SQLite backup API，避免运行中只复制主文件而遗漏 WAL 中的数据。
首次启动以 `PATCHKIT_ADMIN_USER`（默认 `admin`）和初始化哈希创建管理员。
之后修改初始化环境变量不会覆盖数据库中的密码，也不会创建第二个管理员。
账号名做 Unicode NFKC 和大小写归一化后比较，同名账号不能重复创建。
账号名允许 3–64 个字母、数字、点、下划线或短横线。

用户可以从登录页进入 `/register` 申请注册，或由管理员通过 API 创建账号：

| 接口 | 权限与行为 |
| --- | --- |
| `POST /api/auth/register` | 提交 `username`、`password`、`password_confirm`，创建待审核普通账号 |
| `POST /api/auth/login` | 使用数据库中的用户名、密码登录 |
| `GET /api/auth/session` | 返回 `user.id`、用户名、管理员状态、过期时间和 CSRF token |
| `POST /api/auth/logout` | 当前账号退出当前设备 |
| `POST /api/auth/logout-all` | 当前账号退出所有设备 |
| `POST /api/auth/password` | 提交 `current_password`、`new_password`，成功后重新登录 |
| `GET /api/users` | 管理员查询账号列表，不返回密码哈希 |
| `POST /api/users` | 管理员提交 `username`、`password`，创建普通用户 |
| `PATCH /api/users/{user_id}` | 管理员提交 `status`、`group_ids` 审核和授权，也兼容 `is_active` 布尔值 |
| `GET /api/groups` | 管理员查看当前全部文件组，普通用户只能查看自己获准访问的组 |

后台请求可从 `request.state.user` 取得当前用户的稳定 ID、用户名及角色。
最后一个启用的管理员不能被停用。账号列表不返回密码哈希。

### 注册、审核和授权

1. 用户打开 `/register`，输入账号、密码及确认密码。提交成功后显示等待审核，
   不会自动登录，也不能自行指定管理员角色、账号状态或文件组。
2. 管理员登录查看器后，点击工具栏“账号管理”进入 `/admin/users`。待审核账号排在前面。
3. 勾选允许访问的文件组，点击“通过审核并保存授权”；也可以拒绝申请。
4. 用户重新登录后，切片列表仅显示授权组及自己的上传文件。通过审核但未分配组的账号，
   可以管理自己的上传和裁剪，看不到共享切片。
5. 管理员可以调整授权、停用或重新启用账号。修改组授权立即影响后续请求；停用账号
   会撤销其全部会话。待审核和被拒绝的账号使用正确密码登录时会得到相应提示。

账号状态为 `pending`（待审核）、`active`（启用）、`rejected`（未通过）、
`disabled`（停用）。管理员 API 创建的账号为启用状态，初始没有文件组授权。
注册接口要求同源 Origin，每个来源 IP 每分钟最多 5 次有效注册尝试，全站每分钟最多
60 次；超出返回 `429`。数据库自动升级到版本 2，保留既有账号、密码和未过期会话，
已有普通账号需要由管理员分配文件组。

### 文件组及个人文件存储

将 `--slide-dir` 指向共享根目录，其下一层文件夹为组：

```text
slides/                  # --slide-dir 指向这里
  breast/                # 文件组 breast
    case01.svs
    batch01/case02.svs    # 继承 breast 组权限
  prostate/              # 文件组 prostate
    case03.svs
uploads/
  legacy.svs             # 管理员的历史及新上传文件
  <普通用户ID>/
    <随机文件ID>.svs
crops/
  legacy.png             # 管理员的裁剪文件
  <普通用户ID>/
    crop.png
```

文件组仅从配置的扫描根目录发现；根目录直接放置的切片、通过 `--slide` 注册且不属于
任何组的文件，仅管理员可见。多个扫描根目录时，组名同时显示根目录名以便区分。
组授权存入 SQLite，使用服务端目录的文件系统身份关联，目录重命名可以保留授权。
新建其他目录不会仅因名称相同而匹配原目录身份；重建或迁移文件目录后应在管理页检查授权。
隐藏顶层目录和顶层目录符号链接不作为可授权组，文件符号链接按实际目标检查访问权限。

普通用户上传文件使用随机 ID，在数据库记录所属用户，重启后恢复个人上传列表；
同名上传不会互相覆盖。管理员沿用 uploads、crops 根目录，普通账号使用用户 ID 子目录。
管理员可以查看所有共享文件组，但业务接口不会开放其他账号的个人上传、裁剪或裁剪任务。
授权同时覆盖切片元数据、IIIF 信息和瓦片、掩码、检测结果、裁剪任务，不能通过复制 URL
绕过。裁剪图片、单个和批量下载、删除均限制在当前账号目录内。

撤销共享组权限后，用户无法继续读取该组的切片和图层，已生成到个人 crops 的图片仍归
用户所有并保留访问权限。正在执行的裁剪可以完成；服务重启后保留图片，内存裁剪任务
进度不恢复。关闭认证的 `--no-auth` 模式沿用原有全局文件行为。

Docker Compose 将数据库持久化到独立 `auth-data` 卷，普通重启保留用户和会话；
删除这个卷会丢失账号及会话。Windows 服务保存到 `%BASE%\data\auth.sqlite3`。
升级后原有内存会话需要重新登录一次，此后使用数据库会话。

## 嵌入 Python 服务

`create_app` 为现有嵌入调用保留显式配置入口，不会自动读取环境变量：

```python
from wsi_patchkit.web import create_app
from wsi_patchkit.web.auth import AdminAuth

app = create_app(slides={}, auth=AdminAuth.from_env())
```

显式构造配置时指定数据库路径：

```python
auth = AdminAuth("admin", password_hash, database_path="data/auth.sqlite3")
app = create_app(slides={}, auth=auth)
```

`AdminAuth(..., database_path=None)` 使用临时内存 SQLite，适合测试；持久化服务必须
指定路径，或者调用默认使用持久化路径的 `AdminAuth.from_env()`。

`create_app(..., auth=None)` 默认关闭认证，仅适合测试、本机使用或已有外部鉴权的应用。

参考：[OWASP 密码存储](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)、
[会话管理](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)、
[Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https)。
