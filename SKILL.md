---
name: feishu-card-progress-hook
description: Use when 排查/维护飞书自动进度卡片 hook（agent:start/step/interim/end 系统级卡片，含旁白机制）。
version: 3.3.0
author: 快快 (Hermes assistant for Yinsen)
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [feishu, hook, card, progress, 自动卡片, 进度, 系统级, 旁白]
    related_skills: [lark-card-stream, hermes-agent]
---

# 飞书自动进度卡片 Hook（v3.3：旁白机制 + 缓存命中率 + 成本估算）

Hermes gateway 原生 Hook 系统（`~/.hermes/hooks/`）在**系统层面**自动为飞书长任务创建/更新/完成交互卡片，**不依赖模型自觉**。v3 核心：**卡片步骤显示模型旁白（人话）**，命令反推摘要仅作兜底。**v3.1 新增：finish 卡片尾部显示耗时 + token 统计**。**v3.2 新增：缓存命中率（☁NN%）+ 人民币成本估算**（方法学自 DeepSeek Harness token-meter）。**v3.3（2026-08-19 定稿）：数据源改为精确 usage 采样（`_last_usage_dict`），修复多路径下 input=0/命中率恒 100% 的问题**。

## When to Use

- 排查"飞书长任务没自动出卡片/卡片没完成/卡片显示命令字符"问题
- 排查"卡片没有 token 统计/统计是 0"问题
- 维护/升级 feishu-card-progress hook（改规则、加固、验证）
- 想了解 Hermes 事件驱动 hooks（agent:start/step/interim/end）的用法
- 手动验证 hook 是否正常工作
- `hermes update` 后检查 run.py 补丁是否还在

## 架构（v3.1）

```
模型过程旁白 (interim narration)
  → Hermes run.py 补丁 emit "agent:interim" 事件（总是绑定回调，不受 interim_assistant_messages 影响）
  → feishu-card-progress hook 监听 agent:interim
  → handler.py 写入卡片步骤（旁白优先，替换命令摘要）

任务结束
  → run.py 补丁在 agent:end 事件附带 tokens/input_tokens/output_tokens/api_calls/turn_seconds
  → handler.py finish 时追加 "⏱️ 时长 · X,XXX tokens · N 次调用" 统计尾部
```

**关键依赖：run.py + conversation_loop.py 补丁**（见 `PATCH.md`）——`hermes update` 可能 stash 丢失，更新后必须检查：
```bash
grep -n 'emit("agent:interim"' ~/.hermes/hermes-agent/gateway/run.py   # 补丁1 应命中（旁白桥接）
grep -n "interim_assistant_callback = " ~/.hermes/hermes-agent/gateway/run.py  # 补丁2 应是无 if 的直接绑定
grep -n "_last_usage_dict" ~/.hermes/hermes-agent/gateway/run.py        # 补丁3 应命中（精确 usage 读取）
grep -n "_last_usage_dict" ~/.hermes/hermes-agent/agent/conversation_loop.py  # 补丁4 应命中（usage 保存）
grep -n "cache_read_tokens" ~/.hermes/hermes-agent/gateway/run.py        # 补丁5 应命中（cache 字段传递）
```
若丢失 → 按 `PATCH.md` 里的 diff 手动恢复 → 重启 gateway。

## 文件布局

```
~/.hermes/hooks/feishu-card-progress/
├── HOOK.yaml      # name/description/events/platforms 声明（含 agent:interim）
├── handler.py     # 核心逻辑（v3：旁白优先 + 多工具整体替换 + 卡片精简）
└── PATCH.md       # run.py 补丁 diff 存档（update 后恢复用）
~/.hermes/cache/hook_card_state.json   # 运行状态
~/.hermes/logs/hook_card_progress.log  # hook 日志
```

依赖：`~/.hermes/scripts/card-stream.py`（卡片发送/更新/完成）

## 事件流

| 事件 | 触发时机 | hook 动作 |
|---|---|---|
| `agent:start` | 任务开始 | 记录会话（task/started） |
| `agent:step` | 每次工具调用后 | 追加命令摘要（兜底）+ 计数；iteration≥2 建卡/更新 |
| `agent:interim` | 模型旁白产生（run.py 补丁 emit） | **旁白优先**：替换末尾连续命令摘要 |
| `agent:end` | 任务结束 | finish 卡片（绿色）+ 清理状态 |

## 触发规则（v3 定稿）

| 条件 | 行为 |
|---|---|
| 平台 ≠ feishu | 跳过 |
| session_id 前缀 `cron` / `deleg` | 跳过（cron 自有投递；子代理避免私聊混乱） |
| iteration < 2（前 2 次 API 调用） | 不创建卡片（短任务不打扰） |
| 第 3 次工具调用起 | 自动创建卡片 `🔄 任务处理中` |
| 工具调用更新 | 节流 1s，避免刷爆 API |
| interim 旁白 | 替换末尾连续命令摘要（多工具整体替换）；每条旁白独立成行 |
| `agent:end` | 任务结束 | finish 卡片（绿色）：`✅ 任务完成`，内容=步骤列表 + **统计尾部**（耗时+token+调用次数），清理状态 |
| 30 分钟无更新（僵尸） | 入口惰性 GC 自动 finish `⚠️ 任务中断` 并清理 |

## 卡片内容（v3 精简版）

**用户 2026-08-14 定稿**：卡片**不显示**：
- ❌ 原始指令（任务名）——已去掉
- ❌ "**执行摘要**"标题——已去掉
- ❌ "共 X 次工具调用 · 已完成 Y 步"统计——已去掉

卡片**只显示**：
```
- 模型旁白（人话）
- 另一条旁白
- 命令摘要（无旁白时兜底）
   （空一行）
⏱️ 67s · ↑12,345 ↓678 ☁92% · ¥0.12 · 4 次调用   ← finish 统计尾部（v3.2：命中率% + 人民币成本）
```

**注意**：
- 步骤前缀：内部用**不可见标记**（NARRATION="\x01"）区分旁白 vs 命令摘要，build_content 输出时去掉——卡片不显示 emoji
- 列表：用飞书官方 markdown 无序列表 `- ` 前缀（比 `•` 兼容性广），每行独立
- 旁白截断：`clip_line(text, max_len=50)` **单行截断**（不产生 `\n`，超长找自然断点加 `…`）——⚠️ 不要用多行截断，`\n` 会破坏列表缩进
- 每条旁白**独立成行**（不合并，避免 `… …` 难看）

## Token 统计机制（v3.1，重点坑位）

1. **run.py 补丁第 3 处**（agent:end emit 前）：从 `agent_result` 取 token 字段附带进事件
2. **⚠️ 关键坑**：`agent_result["total_tokens"]` 可能是 **0**（DeepSeek 等 provider 的 usage 响应不带 total_tokens），但 `input_tokens`/`output_tokens` 有值！
   - **修复**：`tokens = total_tokens or (input_tokens + output_tokens)`（CanonicalUsage 标准定义）
   - 诊断方法：`grep "agent:end usage ctx" ~/.hermes/logs/gateway.log`
3. **⚠️ 另一个坑**：`self._running_agents.get(session_key)` 在 agent:end 时**已拿不到 agent**（已释放），必须用 `agent_result`（finalize_turn 返回值，含 input/output/total/api_calls 字段）——不要走 `_running_agents`
4. **agent_result 来源**：conversation_loop 的 `finalize_turn` 返回值（有 total_tokens 键）或 codex_runtime 的 usage_result（**无 total_tokens 键**，只有 input/output）——所以 total 兜底相加是通用解
5. handler 端：`_tokens = int(ctx.get("tokens") or 0)`，>0 才追加显示；`_dur = int(ctx.get("turn_seconds") or duration)`
6. **2026-08-15 单轮增量**：卡片显示**本轮新增消耗**（session 累计 - 上轮基线），不再是会话累计（跨天长会话累计会到几千万，失真）。gateway 用 `self._token_baselines[session_key]` 记录每轮基线，`tokens = in_delta + out_delta`；`_evict_cached_agent` 时清理基线防泄漏（补丁第 4 处）
7. **缓存命中现实（2026-08-15 实测）**：`☁`（cache_read）显示真实数据——CLI 短会话 DeepSeek 缓存命中正常（22K-45K），但 gateway 长会话命中趋近 0（服务端对「系统提示+完整历史+动态工具结果」结构的前缀缓存命中率低）。**命中低就显示少/不显示，这是正确的**，不要为显示而改代码。直连官方 `deepseek` provider（`api.deepseek.com/v1`）后短会话缓存正常；中转（custom/Anthropic 协议）缓存永远 0

8. **v3.3 缓存命中率 + 成本（2026-08-19，方法学自 DeepSeek Harness token-meter，最终定稿）**：
   - **显示形式**：`☁NN%`（命中率 = cache_read / (uncached_input + cache_read + cache_write)），与官方 `cacheHitPercent` 公式完全一致
   - **成本估算**：hook 内置 `MODEL_PRICING`（人民币价目：V4 Flash 输入¥1/M 输出¥2/M 缓存¥0.1/M；V4 Pro 3.2/6.4/0.32），`¥0.xxxx` 显示
   - **✅ 最终正确方案（精确采样）**：conversation_loop 每次 API 调用后把**精确的 canonical_usage**（uncached/cache 互补桶）保存到 `agent._last_usage_dict`（`try: agent._last_usage_dict = usage_dict`），gateway 优先读它（`_last_usage_exact=True` 时直接透传，无基线计算）——对齐 dsh token-meter 的 `addReplacing` 语义（后采样替换前值，防双计）
   - **❌ 不要用**：`session_input_tokens`/`session_prompt_tokens` 做差值（**多路径下某些 agent 不累加 → input 恒 0、命中率恒 100%**，2026-08-19 实测踩坑）；也不要用 `last_prompt_tokens` 减缓存（它只是**最后一次调用**的 prompt，与 cache 累计口径不一致 → input 算成 0）
   - **数据链路**：`_run_agent_inner` 返回的 agent_result 原本没有 cache 字段 → gateway/run.py 两处返回 dict 加了 `cache_read_tokens`/`cache_write_tokens`/`_last_usage_exact`（从 `agent._last_usage_dict` 读取）
   - **⚠️ 命中率 100% 是真实数据，不是 bug**：DeepSeek 长会话前缀缓存命中率确实极高（日志实测：input=224 + cache_read=318848 = 99.9%）。历史 prompt 几乎全命中，只有新增少量 token 计全价——这正是 DeepSeek 省钱的核心优势，显示 100% 说明统计正常。**不要为"显得合理"改数据**
   - **测试注意**：delegate 子代理任务**不发卡片**（session_id 前缀 deleg 被跳过），验证要在主会话跑长任务

## 旁白机制细节（坑位）

1. **run.py 补丁 2 处**：① `_interim_assistant_cb` 内 emit `agent:interim` + `if not _want_interim_messages: return` ② `agent.interim_assistant_callback` 总是绑定（不能 `if _want_interim_messages else None`，否则 `interim_assistant_messages: false` 时旁白完全不产生）
2. **旁白是"完整消息"**（不是流式分片）：`_emit_interim_assistant_message` 每条完整 assistant 消息调一次，handler 无需分片合并
3. **codex runtime 有 `show_commentary` 开关**（默认 true，受 `display.show_commentary` 配置）；若配置了 false 会断旁白链路
4. **顺序**：真实到达顺序是 step（工具调用）→ interim（模型旁白），interim 负责替换命令摘要
5. **多工具整体替换**：一个 step 带多个工具会追加多条命令摘要，interim 把**末尾连续非旁白条目全部替换**成一条旁白（P1 修复）
6. **旁白截断**：`clip_line(text, max_len=50)` 单行截断（2026-08-15 修复——之前 `clip_multiline` 产生 `\n` 破坏 `- ` 列表缩进，导致渲染混乱）

## 手动验证（无真实任务时）

```bash
# 1. Hook 加载验证
cd ~/.hermes/hermes-agent && ./venv/bin/python -c "
from gateway.hooks import HookRegistry
reg = HookRegistry(); reg.discover_and_load()
print([h['name'] for h in reg.loaded_hooks])
"

# 2. 模拟完整事件流（会真发卡片到 chat_id）
cd ~/.hermes/hooks/feishu-card-progress && python3 -c "
import handler
def fire(ev, **kw):
    base = {'platform':'feishu','session_id':'test-x'}
    base.update(kw); handler.handle(ev, base)
fire('agent:start', chat_id='oc_xxx')
fire('agent:step', iteration=0, tool_names=['terminal'])
fire('agent:step', iteration=1, tool_names=['terminal'])
fire('agent:step', iteration=2, tool_names=['read_file'])
fire('agent:interim', text='这是模型旁白测试')
fire('agent:end', response='结果')
"

# 3. 查看状态缓存
python3 -c "import json; print(json.load(open('$HOME/.hermes/cache/hook_card_state.json')))"
```

## 排查

- **卡片没出现**：查 `hook_card_progress.log`；查 `hook_card_state.json` 是否记录到该 session；确认 gateway 重启过（hook 启动时加载）
- **卡片显示命令字符而非旁白**：先查 run.py 补丁是否还在（`grep emit("agent:interim"`）——大概率是 update 后补丁丢了
- **卡片没有 token 统计**：查 `grep "agent:end usage ctx" ~/.hermes/logs/gateway.log`——若 `tokens: 0` 说明 total_tokens 键为 0（DeepSeek 等 provider 不带），需要 `total_tokens or (input+output)` 兜底（v3.1 已修）；若 `usage ctx: {}` 说明 agent_result 里没字段，查 run.py 补丁是否还在
- **卡片 input=0 / 命中率恒 100%**：说明走了 session 累计差值但该路径不累加（codex runtime 等）→ 检查 `_last_usage_dict` 补丁是否在（`grep -rn "_last_usage_dict" gateway/run.py agent/conversation_loop.py`）；若在则看是否 gateway 未重启
- **命中率 100% 是正常现象**（非 bug）：DeepSeek 长会话前缀缓存命中率极高（日志实测 input=224 + cache=318848 ≈ 100%）。历史 prompt 几乎全命中，只有新增 token 计全价——显示 100% 说明统计正常，不要改数据
- **finish 统计尾部只有时长没有 token**：同上——`_tokens = int(ctx.get("tokens") or 0)` 为 0 时不显示
- **gateway 重启后 hook 没加载**：检查 `ps aux | grep gateway` 和 `gateway.pid`
- **只出现"🔄 任务处理中"不更新**：命中节流（1s）或 `replace` 失败——看 log
- **飞书连接超时**：重启后偶发 `feishu connect timed out after 30s`，等它重连即可，不是 hook 问题

## Gateway 重启的正确姿势

- `hermes gateway run --replace` 是新进程 SIGTERM 旧进程的正常接管（日志 `Received SIGTERM as a planned --replace takeover` 正常）
- **不要**在命令里带管道（如 `| grep`），会导致新进程异常退出
- 被安全拦截（gateway 内不能自杀）时：`kill <gateway_pid>` 让 launchd KeepAlive 自动拉起（`launchctl print gui/$(id -u)/ai.hermes.gateway` 确认 keepalive）
- 重启后飞书可能 30s 连接超时，属正常重连

## 与 lark-card-stream skill 的关系

- `lark-card-stream`：**手动**发卡片（agent 主动调 card-stream.py）
- `feishu-card-progress-hook`：**系统自动**发卡片（hook 事件驱动）
- 两者互补：hook 保证"必有卡片"，手动卡片用于特殊场景（进度条、明确步骤）
