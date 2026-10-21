# Ubuntu systemd 文件生成器

`render_units.py`仅使用Python标准库，可在Mac上准备Ubuntu部署文件。它只向新输出目录写文件，不安装依赖、不执行systemctl、不读取凭据、不启动服务或云资源、不申请权限，也不配置公网访问。

示例（所有路径必须为绝对路径）：

```bash
python3.12 deploy/render_units.py \
  --prefix '/opt/h3-api' \
  --runner-dir '/data/h3-cpu' \
  --model-dir '/data/h3-cpu/model' \
  --model-python '/data/h3-cpu/.venv/bin/python' \
  --data-dir '/var/lib/h3-api' \
  --service-user root \
  --output-dir '/tmp/h3-systemd-review'
```

`--prefix`是API包安装目录，服务使用其中独立的`.venv-api/bin/python`；模型仍使用`--model-python`指定的原环境及现有权重。路径中的空格、引号、反斜杠、`$`和`%`按对应配置语法保留；相对路径、`..`和换行/控制字符被拒绝。安装指令中的路径另按shell规则转义。

输出内容：

- `h3-api.service`：仅监听`127.0.0.1:30010`。
- `h3-worker.service`：串行worker，`KillMode=control-group`、`TimeoutStopSec=240s`。
- `h3-api.env.example`：不含API密钥的systemd配置示例。
- `INSTALL.txt`：供操作者审阅和选择执行的安装、私有密钥创建及手动运行步骤。

两个unit均采用`Restart=no`、`UMask=0077`，没有`[Install]`节，也没有互相拉起服务的依赖，不注册开机启动。`WorkingDirectory=/`是有意设置：API安装在独立venv里，状态路径均为绝对路径；模型后端另将子进程工作目录设为runner目录。没有添加会隐藏既有模型目录的`ProtectHome`或`PrivateTmp`设置。

`--service-user`默认为`root`，用于兼容已有试验机，并非通用生产建议。可改为一个**已存在**的普通服务账号；需由操作者确认该账号对API、runner、权重、模型venv和来源审计文件的读/执行权限，以及对完整状态目录的写权限。生成器不创建账号或自动更改模型目录权限。

普通配置与密钥分别位于目标主机的`/etc/h3-api/h3-api.env`和`/etc/h3-api/h3-api-secret.env`。密钥由操作者单独私有创建；生成器不会取用当前环境中的`H3_API_KEY`。缺少任一必需文件时服务启动失败，不能依赖空密钥启动。环境文件供systemd读取，不应直接source到shell。

安装后仍需在目标Ubuntu主机上审阅、运行`systemd-analyze verify`并手动进行受控联调。内容测试不等于真实systemd或API到H3联调。手动启动worker可能处理已有排队任务；中断任务没有断点续推，也不会自动重算。停止服务、作业超时或OS关机均不等于停止云端计算计费，云资源收尾与存储维护仍由操作者负责。
