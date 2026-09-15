# HelloFish 云端服务

`server.py` 是一个独立的、带账号密码登录的贡献记录服务。它为每个账号使用独立 SQLite 数据库，桌面端可通过扫描任务的“云端上传”选项写入 `/api/records/upload`。

启动示例：

```powershell
python cloud/server.py --host 0.0.0.0 --port 8787 --username admin --password "请替换为强密码"
```

浏览器访问 `http://服务器地址:8787/`，使用相同账号登录。生产环境建议放在 HTTPS 反向代理后，并使用防火墙限制管理端口。用户密码以 PBKDF2-SHA256 哈希保存在 `cloud/data/users.json`，记录保存在 `cloud/data/accounts/`。

桌面端配置：在 MXU 的“云端上传”选项填写服务地址（例如 `http://server:8787`）、账号和密码。服务地址也可以直接填写完整上传接口地址。

## VPS 部署

发布工作流 [`.github/workflows/cloud-image.yml`](../.github/workflows/cloud-image.yml) 会在推送 `v*` 标签时构建并发布 GHCR 镜像：
`ghcr.io/bigfishwuhu/hellofish-cloud:latest`。也可以在 GitHub Actions 页面手动运行工作流。

在 VPS 上执行：

```bash
mkdir -p hellofish-cloud && cd hellofish-cloud
curl -fsSLO https://raw.githubusercontent.com/BigFishWuhu/HelloFish/main/cloud/docker-compose.example.yml
cp docker-compose.example.yml docker-compose.yml
printf 'HELLOFISH_CLOUD_USERNAME=admin\nHELLOFISH_CLOUD_PASSWORD=replace-with-a-long-random-password\n' > .env
docker compose pull
docker compose up -d
```

然后通过 `http://VPS 地址:8787/` 登录。建议在 VPS 上使用 Caddy/Nginx 配置 HTTPS，并只对外开放反向代理端口；`cloud/data` 卷需要纳入备份。
