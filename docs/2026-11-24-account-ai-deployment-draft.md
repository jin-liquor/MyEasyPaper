# MyEasyPaper 账户 AI 部署草案（未发布）

**不要照本文修改现网。** 配套源码与生产 Compose 配置已提交，但这次更新未发布生产镜像，也未部署。只有在配套镜像发布并以拟上线版本完成预生产验证后，本文才适用。当前版本仍以[现行部署指南](DEPLOYMENT.md)为准。功能与本地验证范围见[账户 AI 与 PDF 翻译更新说明](2026-11-24-account-ai-and-pdf-translation.md)。

## 新装与已有实例升级

- 全新 Linux 实例：在对应发布目录按现行指南运行 `python3 scripts/prepare_server.py --domain papers.example.com`。新版脚本在项目根目录创建权限为 `600` 的 `.env`，其中含随机数据库密码、JWT 密钥和初始账户 AI 加密密钥环（活动版本 `v1`）；文件已存在时拒绝覆盖。
- 已有实例：先备份并记录代码版本、镜像与配置。不要重新初始化或替换 `.env`；保留数据库密码、JWT 密钥和任何已有账户 AI 加密密钥。若从未配置账户 AI 加密密钥，在服务器的持久配置中新增密钥环；若只有旧单密钥配置，保留 `EASYPAPER_CREDENTIAL_ENCRYPTION_KEY` 原值，可暂时沿用，并按下文迁入密钥环。不要重新生成同名密钥覆盖原值。
- 新版生产 Compose 仅向后端传入 `EASYPAPER_CREDENTIAL_ENCRYPTION_KEYS`、`EASYPAPER_CREDENTIAL_ENCRYPTION_ACTIVE_VERSION` 和兼容用的旧单密钥变量。生产后端缺少有效密钥配置时拒绝启动。不要提交、发送或打印 `.env` 或密钥；检查 Compose 配置时使用 `config --quiet`。
- 发布后由每个账户在网页「设置 → AI」配置自己的供应商和默认模型。全局 `LLM_*` 和 Codex 都不能为网页账户提供 AI 回退；账户 AI 不继承下载代理，后端需要直连供应商的公网 HTTPS 端点。未配置 AI 的账户仍可登录、阅读已有论文；依赖 AI 的操作会提示未配置。连接测试会向选定模型发送最小真实请求，可能计费。

## 迁移旧版 Mac Worker

1. 升级前记录旧 Worker 与需要重试的任务，安排维护窗口。新版服务器配置将 `EASYPAPER_GLOSS_PREPARATION` 和 `EASYPAPER_KNOWLEDGE_PREPARATION` 都设为 `server`。网页点读与知识整理改由后端使用任务所属账户的默认模型，不再按 `remote_worker` 或 `user_choice` 派发。不要把供应商 API Key 或服务器加密密钥复制到 Worker。
2. 切换时停用旧 Mac 上的 LaunchAgent；仓库安装器使用 `com.easypaper.gloss-worker`，可在运行 Worker 的 macOS 用户下用 `launchctl bootout "gui/$(id -u)/com.easypaper.gloss-worker"` 卸载当前登录会话中的任务。登录自启也要停用；如还有独立运行的 Worker，确认它们不再轮询。
3. 新后端启动时会清除旧队列中待领取及已领取任务的租约并标记失败，提示用户在网页重试，不会自动转派。已保存的点读结果不因此删除。确认账户默认模型可用后，在网页重新发起受影响的点读或知识整理任务。

## 轮换账户 AI 凭据加密密钥

1. `EASYPAPER_CREDENTIAL_ENCRYPTION_KEYS` 是「版本 → Base64 编码的 32 字节随机密钥」的 JSON 映射。如果原来只用单密钥，先将原值加入映射，命名为 `v1`，并以 `v1` 为活动版本验证原有凭据可读。旧格式密文只用 `v1` 解密。
2. 加入新版本并保留旧版本，先让后端加载完整密钥环，再将 `EASYPAPER_CREDENTIAL_ENCRYPTION_ACTIVE_VERSION` 切换到新版本。确认运行中的后端已应用新配置，所有新写入都使用新版本；重封装前备份数据库并校验备份，另在受限存储保留解密历史备份所需的密钥。
3. 在对应发布目录用目标后端容器的数据库连接和密钥环运行 `rewrap-ai-credentials`。以下命令适用于单一生产 Compose 文件；如使用覆盖文件或自定义项目名，保持与运行中的服务一致。先预检，确认无错误后才正式执行，最后再预检：

   ```bash
   docker compose -f docker-compose.prod.yml exec -T backend python -m app.cli rewrap-ai-credentials --dry-run --batch-size 100
   docker compose -f docker-compose.prod.yml exec -T backend python -m app.cli rewrap-ai-credentials --batch-size 100
   docker compose -f docker-compose.prod.yml exec -T backend python -m app.cli rewrap-ai-credentials --dry-run --batch-size 100
   ```

4. 预检不写库；正式命令分批读取、逐条提交，检测到并发更新时不覆盖较新的凭据。命令不输出凭据；失败或有并发冲突时返回非零。中断后可用同样的配置重跑，已校验且使用活动版本的记录会跳过。只有最终预检成功、`would_rewrap=0`、`failed=0`、`conflicts=0`，且确认所有写入进程均已切换，才能从在线密钥环退役旧版本。只切回活动版本时仍需完整密钥环；如果要回滚旧镜像，先验证旧版本能否读取重封装后的数据，否则按演练过的步骤恢复兼容的备份和密钥。

## 上线前验收

用预生产实例验证生产 Compose 和 PostgreSQL、双账户配置隔离、无账户配置时不回退、选定模型的真实连通性、小 PDF 翻译、旧任务网页重试，以及加密密钥轮换和备份恢复。仅核对源码或通过容器健康检查，不能替代这些验收。
