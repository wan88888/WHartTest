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
docker compose build backend frontend weixin-plugin-host actuator function-sandbox
docker compose up -d
bash deploy-scripts/init-local.sh
```

后端和技能沙箱复用 `wharttest-backend:local` 镜像；执行器以无头 Chromium 运行，名称为 `Local Docker Chromium`。`init-local.sh` 同步容器集成 API Key、预置技能和默认提示词，可重复执行。

后端镜像通过 `WHartTest_Django/docker/constraints-runtime.txt` 固定 Kombu 5.5.4，以兼容项目中的 Celery 5.4 与 redis-py 5.2，避免任务进程因 Redis 客户端版本不匹配而退出。

数据库、Redis、Qdrant 使用 Docker 卷；上传文件、聊天记录和执行器产物保存在 `data/`。备份时同时保留 `.env`、`data/` 和数据库卷，勿使用 `docker compose down -v` 删除数据。

这是本机开发部署，仅开放回环地址的 8913（前端）和 8912（后端）端口，沿用本地配置的 Django 调试模式。Docker Desktop 启动后，已有容器会按 `unless-stopped` 策略恢复。

大模型和知识库模型需要在平台配置相应服务地址与 API Key；独立 Docx 编辑服务不包含在本仓库的默认部署栈中。MCP 兼容服务保留为可选 profile。

## 接口自定义函数

`function-sandbox` 独立执行函数试运行、已保存函数和接口钩子。Django 通过专用 Unix 套接字卷提交函数代码和本次调用的参数。容器关闭网络、不挂载业务数据，以普通用户运行；每次调用启动独立进程，seccomp 限制系统调用，并限制 CPU 为 2 秒、实际执行为 4 秒、进程地址空间为 128 MiB、输入输出各为 128 KiB。隔离服务离线时，试运行返回 503。

适合 JSON 参数与结果的纯计算函数，可使用预加载的常用标准库（如 `json`、`hashlib`、`hmac`、`datetime`、`random`、`re`、`uuid`）。读写文件、网络、创建子进程、加载额外依赖或依赖跨调用全局状态的旧函数需要调整。独立部署 Django 与 Celery 时，把同一个套接字目录挂载给其运行进程，可用 `FUNCTION_SANDBOX_SOCKET` 指定路径。请使用此仓库的 Linux 容器启动执行器，以应用同等内核限制。

## 飞书登录

在飞书开放平台的应用「开发配置 → 安全设置 → 重定向 URL」中添加 `http://localhost:8913/auth/feishu/callback`，从同一地址的登录页选择「账号登录 → 飞书登录」。部署到其他域名时，将回调改成对应的 HTTPS 地址。

在根目录私有 `.env` 中设置 `FEISHU_LOGIN_ENABLED=True`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_REDIRECT_URI` 和 `FEISHU_ALLOWED_TENANT_KEYS`（多个企业用逗号分隔），然后执行 `docker compose up -d backend` 加载配置。

首次授权自动创建的账号会加入「普通测试成员」用户组。默认可查看已加入的项目，创建和编辑测试用例、测试套件并记录执行结果。管理员按需添加项目成员，也可在权限管理中调整该组的权限。设为 `FEISHU_AUTO_CREATE_USER=False` 时，管理员需先在 Django 管理后台的「飞书账号关联」中按应用、企业和用户 Open ID 关联现有账号。

需求管理支持按用户身份导入飞书文档的纯文本正文。请在同一飞书应用开通并发布用户身份权限 `docx:document:readonly`；导入知识库文档还需 `wiki:node:read`。用户在上传弹窗提交链接后完成本次授权，回到原项目即可查看导入的需求。
