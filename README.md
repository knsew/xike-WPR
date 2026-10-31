# 二手机批签（PTI）管理系统 · 服务端

Docker 服务端：Python 标准库零依赖 + SQLite 单文件 + Token 鉴权 + 每天备份（最多 5 份循环）。

## 部署（推荐：直接拉镜像，不用 build）

```bash
mkdir pti-server && cd pti-server
# 从本仓库 server/ 里取 docker-compose.pull.yml 改名为 docker-compose.yml，取 .env.example 改名为 .env 并填管理员手机号与强密码
docker compose pull
docker compose up -d
curl http://127.0.0.1:8000/health
```

镜像：`ghcr.io/knsew/pti-server:0.1.0-test`（GitHub Actions 构建并推送，见 `.github/workflows/docker.yml`）。
数据库只在宿主机 `./data` 文件夹里，转移服务 = `docker compose down` → 拷走 `./data` → 新机器上再 `up`。

详细接口、鉴权、备份说明见 [server/README.md](server/README.md)。
