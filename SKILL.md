---
name: feishu-card-progress-hook
description: 排查和维护外部飞书私聊长任务进度卡片，使用原生 gateway hook 与可选旁白 observer。
version: 3.3.0
license: MIT
platforms: [macos, linux]
---

# 飞书进度卡片维护

用于维护本仓库的外部 hook、旁白 observer、卡片 helper 和离线测试。不要修改 Hermes 核心、已部署插件/hooks、凭据、配置或其他 profile；不要恢复 PATCH.md 的历史方案。安装和真实投递属于后续操作者的独立工作，本 skill 不授权发送消息。

## 合同与布局

先阅读 README.md 与 NATIVE_CONTRACT.md。gateway `agent:start` 明确 `chat_type=dm` 才记录任务；第三次循环准备的 `agent:step` 创建卡片；原生 `agent:end` 只确认处理结束，黄色结束态明确显示投递未知。未知 chat type、group/forum、短任务、cron/deleg 不创建卡片。

部署评审时保证 HOOK.yaml、handler.py、card-stream.py、storage.py 在活动 `$HERMES_HOME/hooks/feishu-card-progress/` 同级；observer/ 内两文件对应活动 home 的 `plugins/feishu-card-progress-observer/`。插件使用原生 pre_llm_call 绑定 turn 和 on_interim_message 旁白，不修改 prompt，不启用 reasoning，不拦截聊天投递。不启用插件时仍可使用工具摘要。

所有 lark-cli 命令固定显式 `--profile cli_a93011294a39dbc6`。不要引入环境 profile、全局默认身份、其他身份或 `hermes send` 回退。当前解释器通过 sys.executable 运行 sibling helper。状态按活动 HERMES_HOME，支持 context-local home，日志使用宿主 logging。

## 显示与失败处理

卡片只展示步骤，保留 `- ` 列表、旁白单行截断、最近 30 条折叠和 1 秒节流。旁白按同一 native iteration 替换所有工具摘要；turn 不匹配或已结束则丢弃。不要把原始请求、最终答复、raw provider 错误复制到卡片。

当前原生事件没有完整 outcome/delivery/usage。显示真实经过的墙钟时长及 token、缓存、成本、API 调用“未知”；不要靠工具次数推断 API 调用，不解析 response 来判成功，不把 on_stream_end.finished 当作任务/投递成功。精确 registered model lookup 是计价函数的唯一入口；内置数字只是未经验证的历史快照。

send 前保存一次性标记，超时不重发。replace/finish 采用幂等覆盖，最多三次尝试；失败保留终态后续惰性重试。30 分钟无活动或新 start 标记旧卡中断。无 end 的异常只能等待下一有效事件惰性清理。JSON 损坏失败关闭；锁超时跳过，可能遗漏一条进度。不要为完整显示制造事件/字段或虚假绿色完成态。

## 验证与排查

运行 `python3 -B -m unittest discover -s tests -v`，仅临时 home 和 mocked subprocess/socket。可选真实 native registration/queue 离线测试命令见 README。编译检查使用 compile() 或将 py_compile 输出显式放在隔离目录，不写核心 bytecode。

卡片未创建：检查 start 的 platform/session/chat_type，是否到 iteration≥2，gateway hook 是否加载，send 是否已尝试、失败或超时。旁白缺失：检查可选 observer 是否启用、是否绑定 turn、原生是否产生旁白、是否被异步队列延迟；降级工具摘要是预期行为。结束未更新：检查 retained terminal 状态、宿主日志及 mocked 回归；不要通过真实试发诊断。token/cost 未知属于合同限制，不通过源码补丁“修复”。

改动后运行相关离线回归，更新原生合同记录及文档，只提出未发布版本建议，不自行 commit/push/tag/release。
