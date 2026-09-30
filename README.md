# Feishu Card Progress Hook

Hermes gateway 的外部飞书长任务进度卡片。无需修改核心源码。仅明确的私聊（`chat_type: dm`）在第三次工具循环准备时创建卡片；群聊、论坛、未知类型、短任务、cron 和 deleg 会话不创建卡片。

卡片保留步骤列表、单行旁白、最近 30 条展示和 1 秒更新节流。原始用户请求和最终答复不写入卡片。可选的原生 plugin observer 让旁白优先于同一 iteration 的工具摘要。

当前已核验原生合同不提供任务 outcome、usage 或 delivery 结果。`agent:end` 在普通最终答复投递前触发，因此卡片结束为黄色 **⚠️ 处理结束 · 投递状态未知**，尾部显示耗时及 token、缓存、成本、API 调用“未知”。不从答复文字或工具数量推断成功、用量或调用次数。没有真实投递验收。

## 文件与部署布局

本次实现仅在隔离仓库完成；以下描述供后续部署评审，没有安装到生产。

```text
$HERMES_HOME/hooks/feishu-card-progress/
  HOOK.yaml
  handler.py
  card-stream.py
  storage.py
$HERMES_HOME/plugins/feishu-card-progress-observer/   # 可选
  plugin.yaml       # 来源 observer/plugin.yaml
  __init__.py       # 来源 observer/__init__.py
$HERMES_HOME/cache/
  hook_card_state.json
  card_stream_state.json
```

Hook 文件必须在同一目录，handler 使用当前 `sys.executable` 启动 sibling `card-stream.py`。状态按活动 `HERMES_HOME` 解析；在 gateway 中调用原生 `hermes_constants.get_hermes_home()`，保留 context-local 路由，并显式传给子进程。标准库独立运行时回退到环境变量及 `~/.hermes`。支持 macOS/Linux 的 `fcntl` 锁，无第三方 Python 框架。

Observer 使用原生 `register(ctx)` / `ctx.register_hook()`，按活动 home 找 hook；需由操作者按 Hermes 插件管理流程启用。只注册 `pre_llm_call`（绑定 turn）和 `on_interim_message`（完整旁白），不修改 prompt 或流式输出。未启用、旧核心不支持、没有真实旁白或 queued observer 延迟到 end 之后时，降级为工具摘要。不会构造 `agent:interim` 或 delivery 假事件。

实际运行时依赖 `lark-cli`，所有网络命令固定显式使用 **`--profile cli_a93011294a39dbc6`**，不采用 `LARK_CLI_PROFILE` 或全局默认身份。失败不调用 `hermes send`，避免额外消息及身份漂移。

## 状态和失败语义

- JSON 原子写入；独立 hook/helper 文件锁防止 gateway 与 observer 线程丢失更新，以及 CardKit sequence 冲突。锁等待最多 5 秒，超时跳过当前事件。
- 发送前持久化一次性标记，send 不重试。若远端接收后本地超时或崩溃，可能有一张无法继续关联的卡；优先避免重复发送。
- replace/finish 使用覆盖语义，失败最多重试两次。finish 仍失败时保留终态记录，在后续有效事件惰性重试。
- 30 分钟无活动时，后续有效事件标记旧卡“任务中断”。新 start 也中断旧卡；尚未成功结束的旧卡保留待重试。
- 核心没有稳定的 gateway turn ID 或异常结束/delivery hook。step/end 关联依赖原生 session/current-run guard；plugin 旁白额外校验绑定的 turn ID。缺少 end 的异常任务依赖上述惰性中断，不能实时识别 delivery 失败。
- JSON 损坏或读取权限错误时失败关闭，保留文件并停止卡片操作。日志交给 gateway 的 Python logging，不在 import 时创建文件。

`MODEL_PRICING` 仅保留未经验证的历史 CNY 快照，函数只接受精确注册名，不匹配别名、前缀或日期。未知模型返回 `None`。当前原生链路缺少 usage，终态始终显示成本未知；快照不是已验证的 provider 价格。

## 离线验证

```bash
python3 -B -m unittest discover -s tests -v
```

标准库测试直接 import 实际 handler/helper，以临时 `HERMES_HOME` 和 mocked subprocess/socket 边界运行，不发消息。可选的 native integration 需要可只读访问的 Hermes 源码和已有依赖环境：

```bash
HERMES_NATIVE_SOURCE=/path/to/hermes-agent \
  /path/to/hermes-agent/venv/bin/python -B -m unittest discover -s tests -v
```

该测试使用实际 `HookRegistry`、`PluginContext`、`PluginManager` 和 streaming observer queue；禁止网络连接，mock subprocess 与无关 provider discovery/config 查询。只复制本仓 hook 到临时 home，不扫描已安装插件。

精确源码版本、字段和降级合同见 [NATIVE_CONTRACT.md](NATIVE_CONTRACT.md)。旧核心补丁说明已在 [PATCH.md](PATCH.md) 退休，不应用或恢复。官方参考：[Hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks)、[Plugins](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)。

## License

MIT
