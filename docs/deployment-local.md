# 本机 Docker 部署

访问地址：<http://localhost:8913>。管理员用户名为 `admin`，初始密码保存在根目录 `.env` 的 `DJANGO_ADMIN_PASSWORD` 中。

`.env` 通过 `COMPOSE_FILE` 加载 `docker-compose.local.yml` 和 `docker-compose.deploy.yml`。在项目根目录执行：

```bash
docker compose up -d
docker compose ps
docker compose logs --tail=100 backend
```

停止和恢复（保留数据）：

```bash
docker compose stop
docker compose start
```

源码修改后重新构建：

```bash
docker compose build backend frontend weixin-plugin-host actuator
docker compose up -d
bash deploy-scripts/init-local.sh
```

后端和技能沙箱复用 `wharttest-backend:local` 镜像；执行器以无头 Chromium 运行，名称为 `Local Docker Chromium`。`init-local.sh` 同步容器集成 API Key、预置技能和默认提示词，可重复执行。

后端镜像通过 `WHartTest_Django/docker/constraints-runtime.txt` 固定 Kombu 5.5.4，以兼容项目中的 Celery 5.4 与 redis-py 5.2，避免任务进程因 Redis 客户端版本不匹配而退出。

数据库、Redis、Qdrant 使用 Docker 卷；上传文件、聊天记录和执行器产物保存在 `data/`。备份时同时保留 `.env`、`data/` 和数据库卷，勿使用 `docker compose down -v` 删除数据。

这是本机开发部署，仅开放回环地址的 8913（前端）和 8912（后端）端口，沿用本地配置的 Django 调试模式。Docker Desktop 启动后，已有容器会按 `unless-stopped` 策略恢复。

大模型和知识库模型需要在平台配置相应服务地址与 API Key；独立 Docx 编辑服务不包含在本仓库的默认部署栈中。MCP 兼容服务保留为可选 profile。

## 飞书登录

在飞书开放平台的应用「开发配置 → 安全设置 → 重定向 URL」中添加 `http://localhost:8913/auth/feishu/callback`，从同一地址的登录页选择「账号登录 → 飞书登录」。部署到其他域名时，将回调改成对应的 HTTPS 地址。

在根目录私有 `.env` 中设置 `FEISHU_LOGIN_ENABLED=True`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_REDIRECT_URI` 和 `FEISHU_ALLOWED_TENANT_KEYS`（多个企业用逗号分隔），然后执行 `docker compose up -d backend` 加载配置。

首次授权自动创建的账号会加入「普通测试成员」用户组。默认可查看已加入的项目，创建和编辑测试用例、测试套件并记录执行结果。管理员按需添加项目成员，也可在权限管理中调整该组的权限。设为 `FEISHU_AUTO_CREATE_USER=False` 时，管理员需先在 Django 管理后台的「飞书账号关联」中按应用、企业和用户 Open ID 关联现有账号。

需求管理支持按用户身份导入飞书文档的纯文本正文。请在同一飞书应用开通并发布用户身份权限 `docx:document:readonly`；导入知识库文档还需 `wiki:node:read`。用户在上传弹窗提交链接后完成本次授权，回到原项目即可查看导入的需求。
