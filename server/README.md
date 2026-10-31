# PTI 服务端 · Docker 部署

只含数据库服务，不含 App、不含前端。镜像很小：`python:3.12-alpine` + 一个 `server.py`（Python 标准库，零第三方依赖），约 50MB 级别。

## 部署（推荐：直接拉 GHCR 镜像，不用 build）

```bash
# 部署机上只需要两个文件：docker-compose.pull.yml（改名为 docker-compose.yml）+ .env（由 .env.example 复制并填管理员手机号与强密码）
docker compose pull
docker compose up -d
curl http://127.0.0.1:8000/health
```

镜像：`ghcr.io/knsew/pti-server:0.1.0-test`，由 GitHub Actions（`.github/workflows/docker.yml`）在公开仓构建并推送到 GHCR。
如 GHCR 包是私有可见，部署机先 `docker login ghcr.io`（用有 read:packages 权限的 token）再 pull；包设为公开后可免登录直接 pull。

## 部署（备选：本地 build）

```bash
cd server
cp .env.example .env   # 填真实管理员手机号、强密码、端口
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

- 服务端口默认 8000，可用 `PTI_PORT` 改。
- 首个管理员只在数据库为空且 `.env` 里给了手机号 + 密码时创建；没给就不创建，**没有默认账号、没有默认密码**。
- `.env` 只放宿主机，`.dockerignore` 与 `.gitignore` 都排除了它和 `data/`，不进镜像、不进 git。

## 数据库怎么转移（核心要求）

数据库是 SQLite 单文件，**只在宿主机 `./data/pti.db`，不在镜像里**：

```bash
docker compose down        # 停服务
cp -r ./data /新机器的/server/data   # 拷走整个 data 文件夹
# 新机器上：
docker compose up -d --build
```

实测过：停服 → 拷 data 文件夹 → 用拷来的库再启动，机器、单据（已发货状态）、Token 都还在。

备份也一样：定期拷 `./data` 文件夹就行，不用导数据库。

## App 与服务端通信 + 鉴权

App 不直连数据库，只调 HTTP 接口，鉴权流程：

1. `POST /api/login`，body `{"phone":"手机号","password":"密码"}` → 返回 `token`（随机 64 hex，有效期 7 天）、员工姓名、角色。
2. 之后所有业务接口带头 `Authorization: Bearer <token>`；无 Token、过期、账号已停用，一律 401。
3. 角色在服务端逐接口校验：管理类（账号创建、模拟数据导入、审计查看）只允许管理员；前端隐藏按钮不算权限。
4. 密码存法：PBKDF2-HMAC-SHA256 + 每人随机盐，只存哈希，不存明文。

接口（全部 JSON）：

- `GET /health`（免鉴权，供容器检查）
- `POST /api/login`、`GET /api/me`
- `GET /api/machines?q=&customer=`、`GET /api/machines/{机号}`（含该机申请记录）
- `POST /api/orders {"machine_no":...}`（申请批签，服务端规则裁决：已过保直接 403 拦截，不生成单号；通过则生成 `PTI-YYYYMMDD-序号`）
- `POST /api/shipment/verify {"order_no":...,"machine_no":...}`（发货核验：单号不存在 / 与机号不匹配 / 状态不是已通过，一律拒绝）
- `GET /api/orders?machine_no=`、`GET /api/alerts`（已过保 + 闲置 180 天以上）
- `GET /api/audit`（仅管理员）、`POST /api/admin/users`（仅管理员，创建账号）、`POST /api/admin/seed`（仅管理员，导入模拟机器，测试用）

## 安全注意事项

- 只放内网用；如需从外网访问，必须前面加反向代理 + HTTPS，不要把 8000 直接暴露到公网。
- Token 有效期 7 天，内部工具可接受；如需更严，改 `server.py` 里的 `TOKEN_DAYS`。
- 数据库文件权限建议 600，`data/` 文件夹不要给无关账号读。
- 测试版 App 目前还是内存数据，尚未接本服务端；接通后 App 的登录、查机、申请、核验都要走上面这套接口，App 端不再自己裁决规则。

## 本地实测记录（2026-10-31，Python 直接跑，未在沙箱 build 镜像）

- 登录发 Token、未带 Token 调业务接口 401、在保申请通过生成 `PTI-20261031-001`、已过保申请 403 拦截、错误单号核验 403、正确单号核验通过变已发货、报警页正确列出已过保 + 闲置 334 天的机器，全部通过。
- 转移实测：停服 → 拷 `data` → 用拷来的库启动，机器 2 台、单据已发货状态都在。

## 数据库每天备份（最多 5 份，循环）

- 自动：服务端里有个备份线程，每分钟看一次，每天 `PTI_BACKUP_HOUR` 点（默认凌晨 2 点，可在 `.env` / 环境变量里改）之后、且当天还没备份时，自动备份一次。
- 备份方式：用 SQLite backup API 做一致性备份，不是直接拷正在写的文件；备份文件在 `./data/backups/pti-YYYYMMDD-HHMMSS.db`，跟着 `data/` 文件夹一起转移。
- 循环：每次备份后只留最新 5 份，超了删最旧的，不会把磁盘撑满。
- App 端管理员管理（全部仅管理员，非管理员 403）：
  - `GET /api/admin/backups`：查看备份情况（每份的文件名、大小、创建时间、共几份、上限 5 份、下次自动备份说明）
  - `POST /api/admin/backups`：立即手动备份一次
  - `DELETE /api/admin/backups/{文件名}`：删除某一分备份
  - `POST /api/admin/backups/{文件名}/restore`：恢复到该备份；恢复前会自动先给当前库做一份备份，恢复后请重新登录（Token 以备份时为准）
- App 端备份管理界面（管理页里的“备份”入口，列表 + 手动备份 + 删除 + 恢复）接上面这套接口，等测试版 App 接服务端时一起推，不单独再起一个备份机制。
