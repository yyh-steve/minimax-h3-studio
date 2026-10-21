# 流水线与模型服务接入

本方案由工作台连接自己部署的 H3 生成视频，调用官方 M3 编写分镜和看片。两个服务分别配置地址与密钥；真实 `.env`、素材和任务数据库不要提交到 Git。

## 自部署 H3 与 M3 配置

先按 [部署指南](deployment.md)准备模型与 `h3-api`，再复制 `pipeline/backend/.env.local.example` 为 `.env`。
将模板中的 `M3_PROVIDER=mock` 改为 `official`，填写两个独立密钥，并按需开启 M3 看片：

```dotenv
H3_PROVIDER=local
H3_BASE_URL=http://127.0.0.1:30010
H3_API_KEY=
M3_PROVIDER=official
M3_BASE_URL=https://api.minimax.cn/v1
M3_API_KEY=
M3_TAKE_REVIEW=true
MAX_CONCURRENCY=1
AUTO_RETRY_LIMIT=0
```

`H3_API_KEY` 必须是自部署服务的密钥，至少 24 位无空白 ASCII 字符，不会自动借用官方 `MINIMAX_API_KEY`。
`M3_API_KEY` 填官方 M3 密钥，地址和模型权限以自己的控制台为准。M3 地址包含 `/v1`，H3 V2 地址不附加 `/v1`。
`M3_TAKE_REVIEW=true` 开启生成后的 M3 看片；设为 `false` 时保留技术检查。

H3 所需提示词和参考图发送到自己部署的服务，消耗该主机的计算资源；M3 所需文本、图片及看片视频发送到 M3 API，按服务商规则计费。

本地 provider 在每次创建请求前检查服务的 `/capabilities`，只有对方明确声明单图 Ref2VA 和 `Idempotency-Key` 扩展才提交。`local` 不能指向已知的 MiniMax 官方 API 地址；官方服务应选择 `official`。从其他模式切换到 `local` 时需显式提供该服务的独立密钥。

本地 H3 只接受 `MiniMax-H3`、`768P`、4–15 秒整数时长、一条文本和一张 `reference_image`。使用“参考图”分镜模式；不能用首帧、首尾帧、文生、多参考图或 2K 请求替代。已有分镜若不满足这些限制，应先修改和重新校验，再确认生成。

CPU 成本主要取决于计算时间，不能把“输出秒数预算”当成云费用预算。本地接入默认从单并发、无自动重抽开始；新建项目的 15–60 秒总时长会拆成多个镜头，各镜头仍可能分别耗时数小时。

## 不同机器之间连接

H3 API 默认仅监听云服务器的回环地址。可在运行流水线的电脑上创建 SSH 隧道：

```bash
ssh -N -L 127.0.0.1:30010:127.0.0.1:30010 USER@ECS_HOST
```

把 `USER` 和 `ECS_HOST` 换成自己的登录名与主机。H3 服务的 `H3_PUBLIC_BASE_URL` 和流水线的 `H3_BASE_URL` 都可使用 `http://127.0.0.1:30010`，下载链接也会通过该隧道访问。

两端配置应区分：

| 配置 | 所属进程 | 用途 |
| --- | --- | --- |
| `H3_DATA_DIR` | H3 API / worker | 推理任务库和任务文件 |
| `H3_RUNNER_DIR` | H3 worker | 本仓库 `cpu-runtime` 的绝对位置 |
| `H3_MODEL_PYTHON` | H3 worker | 原模型虚拟环境中的 Python 入口 |
| `PIPELINE_DATA_DIR` | 视频流水线 | 项目、分镜、素材和合成结果 |
| `H3_API_KEY` | 两端 | 同一自部署 H3 服务的鉴权密钥 |
| `M3_API_KEY` | 视频流水线 | 官方 M3 的独立鉴权密钥 |

UI 设置影响当前应用进程；需要重启后仍使用的配置，应写入本机 `.env` 或明确的进程环境变量。不要把服务端 systemd 的 EnvironmentFile 当成 shell 脚本直接执行。

## 长任务与恢复

有任务 ID 时优先恢复查询，不重新创建。自部署模式使用持久化幂等键恢复同一次提交；它是本地服务扩展，不是对官方服务的幂等承诺。官方创建结果未知时，不应按时间和时长猜测绑定到另一条视频。

切换 H3 后端前应先处理现有活动任务，防止用新地址或新身份查询旧任务。下载链接会过期，重新查询成功任务可取得新链接。取消发生时，远端可能已结束生成；取消与删除结果是不同操作，不能把删除结果误称为取消推理。

API 接受 15 秒参数不代表机器能在任意作业时限内完成它。源码模板的模型限额默认 18 小时；历史 5 秒验收曾临时设为 3 小时，长片测试前应复核配置与预算。作业时限、停止 worker、操作系统关机都不代替云平台的计费停机操作。

## 开发配置与其他后端

仓库保留以下 provider 供开发或切换后端使用，本方案的 H3 配置保持为 `local`。

### 模拟服务

```dotenv
H3_PROVIDER=mock
M3_PROVIDER=mock
```

这是 `pipeline/backend/.env.example` 的默认值。分镜、看片结论和视频由测试替身生成，用于开发编辑、确认、队列、质检和合成功能，无需模型密钥。

### 官方 H3 后端

```dotenv
H3_PROVIDER=official
M3_PROVIDER=official
H3_BASE_URL=https://api.minimax.cn
M3_BASE_URL=https://api.minimax.cn/v1
H3_API_KEY=
M3_API_KEY=
```

切换时填写官方服务的密钥和地址。两种官方服务使用同一密钥时，也可使用兼容字段 `MINIMAX_API_KEY`。
官方创建请求超时且没有可靠任务 ID 时，先在服务端核实是否已创建任务，再使用准确任务 ID 恢复查询，避免重复生成。
