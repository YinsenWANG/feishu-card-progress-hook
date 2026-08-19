# Feishu Card Progress Hook

自动为飞书（Lark）长任务生成/更新/完成交互卡片的 Hermes Agent Hook。
系统级事件驱动（`agent:start` / `agent:step` / `agent:interim` / `agent:end`），不依赖模型自觉。

## 功能

- **自动进度卡片**：私聊里 3+ 步的长任务自动弹出交互卡片，实时显示进行到哪一步
- **旁白机制**：卡片步骤显示模型的"人话"旁白（interim narration），而非工具命令
- **完成统计**：卡片完成变绿，附带耗时、输入/输出 token、缓存命中率、成本估算（人民币）
- **不刷屏**：短任务和群聊不出卡片；过程更新收进卡片，结果独占一条消息
- **僵尸保护**：30 分钟无更新的任务自动标记中断

## 架构

```
模型过程旁白 (interim narration)
  → Hermes run.py 补丁 emit "agent:interim" 事件
  → feishu-card-progress hook 监听
  → handler.py 写入卡片步骤（旁白优先）

任务结束
  → run.py 补丁在 agent:end 附带 token 统计
  → handler.py finish 卡片（绿色）+ 统计尾部
```

## 部署

1. 将本仓库文件放入 `~/.hermes/hooks/feishu-card-progress/`
   ```
   ~/.hermes/hooks/feishu-card-progress/
   ├── HOOK.yaml      # hook 声明（事件 + 平台）
   ├── handler.py     # 核心逻辑
   ├── SKILL.md       # 完整 skill 文档（含坑位/排查）
   └── PATCH.md       # Hermes 源码补丁存档（5 处，升级后恢复用）
   ```
2. 按 `PATCH.md` 给 Hermes 源码打 5 处补丁（旁白桥接 + 精确 usage 采样）
3. 重启 gateway 服务

依赖：
- [lark-cli](https://github.com/larksuite/lark-cli)（飞书 CLI，需配置应用凭据）
- `card-stream.py`（卡片发送/更新/完成，本仓库包含）

## 补丁与升级

`hermes update` 可能 stash 掉本地补丁，升级后需检查并按 `PATCH.md` 恢复：

```bash
grep -n 'emit("agent:interim"' ~/.hermes/hermes-agent/gateway/run.py
grep -n "_last_usage_dict" ~/.hermes/hermes-agent/gateway/run.py
grep -n "_last_usage_dict" ~/.hermes/hermes-agent/agent/conversation_loop.py
```

## Token 统计

卡片完成时显示（DeepSeek 直连示例）：

```
⏱️ 67s · ↑12,345 ↓678 ☁92% · ¥0.12 · 4 次调用
```

- ↑/↓：本轮输入/输出 token（uncached input / output，互补桶不重复计数）
- ☁：缓存命中率 = cache_read / (uncached_input + cache_read + cache_write)
- ¥：成本估算（人民币，缓存按折扣价）

统计方法学自 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的 token-meter（精确 usage 采样，addReplacing 语义）。

## License

MIT
