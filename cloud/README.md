# HelloFish 云端服务

`server.py` 是一个独立的、带账号密码登录的贡献记录服务。它为每个账号使用独立 SQLite 数据库，桌面端可通过扫描任务的“云端上传”选项写入 `/api/records/upload`。

启动示例：

```powershell
python cloud/server.py --host 0.0.0.0 --port 8787 --username admin --password "请替换为强密码"
```

浏览器访问 `http://服务器地址:8787/`，使用相同账号登录。生产环境建议放在 HTTPS 反向代理后，并使用防火墙限制管理端口。用户密码以 PBKDF2-SHA256 哈希保存在 `cloud/data/users.json`，记录保存在 `cloud/data/accounts/`。

如果省略 `--password`（或不设置 `HELLOFISH_CLOUD_PASSWORD`），首次访问 Web 页面会显示初始化表单。设置账号和密码后会自动登录，初始化接口只允许成功一次；已有账号仍使用普通登录。

桌面端配置：在 MXU 的“云端上传”选项填写服务地址（例如 `http://server:8787`）、账号和密码。服务地址也可以直接填写完整上传接口地址。

## VPS 部署

发布工作流 [`.github/workflows/cloud-image.yml`](../.github/workflows/cloud-image.yml) 会在推送 `v*` 标签时构建并发布 GHCR 镜像：
`ghcr.io/bigfishwuhu/hellofish-cloud:latest`。也可以在 GitHub Actions 页面手动运行工作流。

在 VPS 上执行（无需配置初始密码）：

```bash
mkdir -p hellofish-cloud && cd hellofish-cloud
curl -fsSLO https://raw.githubusercontent.com/BigFishWuhu/HelloFish/main/cloud/docker-compose.example.yml
cp docker-compose.example.yml docker-compose.yml
printf 'HELLOFISH_CLOUD_USERNAME=admin\n# 可选：留空后在 Web 首次访问时设置\nHELLOFISH_CLOUD_PASSWORD=\n' > .env
docker compose pull
docker compose up -d
```

如果不需要自定义环境变量，可以直接执行 `docker compose pull` 和 `docker compose up -d`，无需创建 `.env`。启动后首次访问 Web 页面即可设置账号和密码。

如果宿主机的 `8787` 端口已被占用，在 `.env` 中指定其他端口，例如：

```bash
printf 'HELLOFISH_CLOUD_PORT=18787\n' > .env
docker compose up -d
```

此时通过 `http://VPS地址:18787/` 访问，容器内部端口仍为 `8787`。

然后通过配置的宿主机端口登录。建议在 VPS 上使用 Caddy/Nginx 配置 HTTPS，并只对外开放反向代理端口；`cloud/data` 卷需要纳入备份。

## GitHub Actions SSH 自动部署

给仓库推送 `v1.2.3` 这类 `v*` tag 时，`install.yml` 会创建 GitHub Release，`cloud-image.yml` 会发布镜像，随后 `cloud-deploy.yml` 会通过 SSH Key 更新 VPS 上的 Docker 服务。也可以单独手动运行部署工作流。首次部署只需准备目标目录；`.env` 为可选项：

```bash
mkdir -p /opt/hellofish-cloud
cd /opt/hellofish-cloud
```

不创建 `.env` 时，容器使用默认端口和空密码启动，首次访问 Web 页面完成账号密码初始化。若需要预置账号或修改宿主机端口，再在该目录创建 `.env`，例如写入 `HELLOFISH_CLOUD_PORT=18787`。

在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 中配置：

- Variables：`CLOUD_VPS_HOST`、`CLOUD_VPS_USER`；可选 `CLOUD_VPS_PORT`（默认 `22`）、`CLOUD_VPS_APP_DIR`（默认 `/opt/hellofish-cloud`）。
- Secrets：`CLOUD_VPS_SSH_KEY`，部署用户对应的 SSH 私钥（通常是 `-----BEGIN OPENSSH PRIVATE KEY-----` 开头的完整内容）；推荐另设 `CLOUD_VPS_KNOWN_HOSTS`，填入 `ssh-keyscan -p 22 your-vps-host` 的结果以校验主机指纹。

将对应公钥加入 VPS 部署用户的 `~/.ssh/authorized_keys`。工作流只上传 Compose 定义并执行 `docker compose pull`、`docker compose up -d`；密码保留在 VPS `.env`，不会写入仓库或工作流日志。
