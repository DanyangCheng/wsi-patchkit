# 管理员登录与 HTTPS 部署

命令行服务默认要求管理员配置，缺少密码哈希时拒绝启动。不要把明文密码放入配置。

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
- 每分钟全局允许 10 次有效格式的登录尝试，超出返回 `429`；这是单管理员共享限额。
- 最多保留 32 个会话，超出时撤销最早创建的会话。再次登录会轮换当前浏览器的会话。
- 会话在单进程内存保存，退出立即撤销；服务重启全部失效。保持一个 Uvicorn worker
  和一个服务实例；扩展多进程或多副本前应接入共享会话及限流存储。
- 修改密码需重新生成哈希，并重启 viewer，以撤销已有会话。
- 认证模式下响应设置 `Cache-Control: no-store`，避免 HTTP 缓存保留受保护图像。
  已下载文件和已显示在浏览器中的图像无法远程撤回。

## 嵌入 Python 服务

`create_app` 为现有嵌入调用保留显式配置入口，不会自动读取环境变量：

```python
from wsi_patchkit.web import create_app
from wsi_patchkit.web.auth import AdminAuth

app = create_app(slides={}, auth=AdminAuth.from_env())
```

`create_app(..., auth=None)` 默认关闭认证，仅适合测试、本机使用或已有外部鉴权的应用。

参考：[OWASP 密码存储](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)、
[会话管理](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)、
[Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https)。
