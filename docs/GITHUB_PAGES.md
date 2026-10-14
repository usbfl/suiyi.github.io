# AlphaLab · GitHub Pages 部署

本仓库基于 2026-10-14 上传的 AlphaLab 完整源码。原始包校验信息保存在 `SOURCE_ARCHIVE_MANIFEST.json`；该文件仅描述原始压缩包，不代表修改后仓库的校验值。

## 地址与功能边界

- 仓库：<https://github.com/usbfl/suiyi.github.io>
- 此仓库的默认 Pages 地址：<https://usbfl.github.io/suiyi.github.io/>
- 用户名是 `usbfl`，仓库名不是 `usbfl.github.io`，所以它是项目站点。
- Pages 发布原项目的 HTML/CSS/JavaScript。行情、模型调用、回测、SSE 和 SQLite 在独立 Python 后端运行。
- 未连接后端时显示连接入口，不提供虚构行情或回测结果。Python 后端未因推送仓库而自动部署。

## 1. 前端发布（沿用现有设置）

仓库已启用 GitHub Pages 的分支发布，并有成功的 `pages build and deployment` 记录。本次沿用 **main / 根目录**，推送即可自动发布，无需改为自定义 Actions。
根目录 `index.html` 是 Pages 入口，`static/index.html` 是 Python 服务的本地入口；`_config.yml` 排除后端、测试与文档等目录。发布资产不包含数据库或密钥。

修改主界面 HTML 后，同步生成 Pages 入口：

```bash
python tools/build_pages.py --root-entry
```

可独立构建只包含前端的发布目录（无需 npm 或第三方 Python 依赖）：

```bash
python tools/build_pages.py
python -m http.server 8080 --directory _site
```

`_site` 必须是新目录。再次构建前移走旧构建产物。
资源使用相对路径，可部署在仓库子路径，也可迁移至自定义域名。
如果更改了仓库 Pages 设置，请恢复 **Settings → Pages → Source: Deploy from a branch → main / (root)**。

## 2. 部署后端

可以使用支持 Docker、HTTPS、持久化磁盘与长连接的服务器。维持 **1 个进程、1 个实例**；这是单用户应用，SQLite 与内存调度不支持多实例。
默认六位 Agent 与所有原有业务逻辑保留。

在服务器项目目录创建 `.env.production`（不要提交）：

```dotenv
ALPHALAB_ALLOWED_HOSTS=127.0.0.1,localhost,api.your-domain.example
ALPHALAB_CORS_ORIGINS=https://usbfl.github.io
ALPHALAB_ACCESS_TOKEN=REPLACE_WITH_A_RANDOM_TOKEN_OF_AT_LEAST_32_CHARACTERS
ALPHALAB_SECRET_KEY=REPLACE_WITH_A_GENERATED_FERNET_KEY
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=
LLM_TIMEOUT=45
LLM_CONCURRENCY=3
LLM_MAX_CALLS=300
```

`ALPHALAB_ALLOWED_HOSTS` 填后端真实主机名。CORS 填前端 **origin**，不包含 `/suiyi.github.io/` 路径。
用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 在服务器生成独立工作区口令，替换示例值。不要使用模型 API Key 充当访问口令。
Linux/Docker 还需生成并配置独立的加密主密钥：`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`。也可用上述已构建镜像执行该命令。保管并备份 `ALPHALAB_SECRET_KEY`，服务重启时保持不变，否则已保存的模型凭据无法解密。Windows 原有 DPAPI 密文不能直接在 Linux 解密，迁移时重新输入模型凭据。

需要公开访问的后端启动时会检查是否配置至少 32 字符的口令。不能使用 `*` 放开所有来源或主机。

```bash
docker compose up -d --build
```

Compose 仅监听服务器 `127.0.0.1:8010`，数据保存在命名卷 `alphalab-data`。
用 Nginx/Caddy 等代理提供 HTTPS。Nginx 的 API 代理需包含：

```nginx
location / {
    proxy_pass http://127.0.0.1:8010;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Authorization $http_authorization;
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

证书、域名与 HTTPS server 配置依服务器实际环境设置。若使用容器托管平台，配置同样的环境变量，并将持久化卷挂到 `/app/data`；卷必须允许 UID 10001 写入。不要将模型密钥设置成 GitHub 仓库公开变量。
健康检查为 `/healthz`。前端使用 `/api/config` 校验服务，所有 `/api/*` 请求（含导出和 SSE）都需要工作区口令。
从公网使用后端本身的界面，也需要在“服务连接”输入该后端 HTTPS 根地址和口令。

## 3. 连接在线前端

打开 Pages 地址，点击“服务连接”，输入后端 HTTPS 根地址与工作区口令。
校验成功后进入原 AlphaLab 工作台。地址保存在当前站点的浏览器本地存储；访问口令只保存在当前标签页的会话存储，并绑定该地址。
模型 API Key 通过连接后的“模型连接”交给后端存储，不写入 Pages 资产。

可选：编辑 `static/deploy-config.js` 中的 `apiBase` 为后端 HTTPS 根地址并提交。该文件只能预置地址，不能包含任何口令或模型 API Key。

## 验证与排错

- 页面资源 404：确认以完整 `/suiyi.github.io/` 地址访问，并选择 main / 根目录发布。
- `401`：工作区口令错误或缺失；重新“服务连接”。
- CORS/网络错误：核对 HTTPS、后端可达性和 `ALPHALAB_CORS_ORIGINS=https://usbfl.github.io`。
- `Invalid host header`：后端域名未加入 `ALPHALAB_ALLOWED_HOSTS`。
- SSE 无更新：关闭代理缓冲，允许持续连接，检查口令与后端进程状态。
- 行情源访问失败：检查后端所在地区的行情网络访问；前端不会替换为生成数据。
- 不运行多 worker，不把此单用户工作区的口令公开给访客。

本地运行仍可使用原来的 `run.ps1` 或 `uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1`；未配置远程托管变量时保留本地访问限制。

官方参考：
- <https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site>
- <https://fastapi.tiangolo.com/tutorial/cors/>
