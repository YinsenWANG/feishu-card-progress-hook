# Hermes run.py + conversation_loop.py 补丁记录（2026-08-14 / 2026-08-19 更新）

**用途**：把模型过程旁白（interim narration）桥接到 hook 事件 `agent:interim`，
让 feishu-card-progress hook 能把它写进飞书进度卡片（不刷屏、旁白优先）。
2026-08-19 新增：精确 token/cache usage 采样（方法学自 DeepSeek Harness token-meter）。

**补丁位置**：
- `~/.hermes/hermes-agent/gateway/run.py`（Hermes 源码，本地修改）
- `~/.hermes/hermes-agent/agent/conversation_loop.py`（2026-08-19 新增）

## 补丁内容（5 处）

### 1. `_interim_assistant_cb` 内（约 5360 行，gateway/run.py）
- **新增**：`display_text` 非空时 emit `agent:interim` 事件给 hooks（携带 platform/session_id/chat_id/text）
- **新增**：`if not _want_interim_messages: return` —— 用户关闭旁白投递时，不发聊天窗（但 hook 事件已发，卡片仍能拿到）

### 2. `agent.interim_assistant_callback` 绑定处（约 5724 行，gateway/run.py）
- **修改**：`= _interim_assistant_cb if _want_interim_messages else None` → `= _interim_assistant_cb`（总是绑定）
- **原因**：`interim_assistant_messages: false` 时原来不绑定回调 → 旁白完全不产生 → hook 拿不到

### 3. `agent:end` emit 处（约 20235 行，gateway/run.py）
- **新增**：从 `agent_result` 取 token 字段，连同 `turn_seconds` 一起带进 `agent:end` 事件
- **⚠️ 关键坑（2026-08-15 修复）**：
  - `agent_result["total_tokens"]` 可能为 0（DeepSeek 等 provider 的 usage 响应不带 total_tokens 键），但 `input_tokens`/`output_tokens` 有值
  - **修复**：`tokens = total_tokens or (input_tokens + output_tokens)`（CanonicalUsage 标准定义）
  - 另一个坑：`self._running_agents.get(session_key)` 在 agent:end 时**已拿不到 agent**（已释放），必须用 `agent_result`（finalize_turn 返回值）
- **2026-08-15 单轮增量**：卡片 token 改为显示**本轮新增消耗**（session 累计 - 上轮基线），用 `self._token_baselines[session_key]` 记录基线

### 4. `_evict_cached_agent` 开头（约 26670 行，gateway/run.py）
- **新增**：session evict 时清理 `_token_baselines[session_key]`，防止基线泄漏

### 5. **精确 usage 采样（2026-08-19 新增，最重要）**
- **conversation_loop.py（约 4016 行）**：每次 API 调用后保存精确 usage：
  ```python
  # 在 usage_dict 构造后追加
  try:
      agent._last_usage_dict = usage_dict  # 后采样替换前值（addReplacing 语义）
  except Exception:
      pass
  ```
  usage_dict 含：`prompt_tokens / input_tokens（uncached）/ output_tokens / cache_read_tokens / cache_write_tokens / reasoning_tokens`
- **gateway/run.py 提取处（约 6335 行）**：
  ```python
  _last_usage = getattr(_agent, "_last_usage_dict", None) or {}
  _last_usage_exact = bool(_last_usage.get("input_tokens"))
  if _last_usage_exact:
      _input_toks = _last_usage.get("input_tokens", 0)
      _output_toks = _last_usage.get("output_tokens", 0)
      _cache_read_toks = _last_usage.get("cache_read_tokens", 0) or 0
      _cache_write_toks = _last_usage.get("cache_write_tokens", 0) or 0
  else:
      # 回退 session 累计差值
      ...
  ```
- **gateway/run.py 两处返回 dict（约 6484/6569 行）**：加 `cache_read_tokens` / `cache_write_tokens` / `_last_usage_exact`
- **gateway/run.py agent:end emit 前（约 20245 行）**：`_is_exact` 时直接透传（无基线计算），否则走 session 差值
- **为什么**：`session_input_tokens`/`session_prompt_tokens` 是会话累计，多路径（codex runtime 等）下某些 agent 不累加 → input 恒 0、命中率恒 100%（2026-08-19 实测踩坑）。精确采样对齐 dsh token-meter 的 addReplacing 语义

## 完整 diff（应用/检查用）

```bash
# 检查补丁是否还在（应输出多处修改）
cd ~/.hermes/hermes-agent && git diff gateway/run.py agent/conversation_loop.py | head -60

# 若 update 后补丁丢失（git stash apply 冲突），重新应用：
# 1. 从本文件手动恢复，或
# 2. 先 cd ~/.hermes/hermes-agent && git stash list 找 stash
```

关键 grep 验证：
```bash
# 应看到：agent.interim_assistant_callback = _interim_assistant_cb（无 if 条件）
grep -n "interim_assistant_callback = " ~/.hermes/hermes-agent/gateway/run.py
# 应看到：emit("agent:interim"
grep -n 'emit("agent:interim"' ~/.hermes/hermes-agent/gateway/run.py
# 应看到：_last_usage_dict（gateway 读取 + conversation_loop 保存）
grep -rn "_last_usage_dict" ~/.hermes/hermes-agent/gateway/run.py ~/.hermes/hermes-agent/agent/conversation_loop.py
# 应看到：cache_read_tokens 传递
grep -n "cache_read_tokens" ~/.hermes/hermes-agent/gateway/run.py | head -5
```

## 依赖的 hook 侧

- `~/.hermes/hooks/feishu-card-progress/HOOK.yaml`：已声明 `agent:interim` 事件
- `~/.hermes/hooks/feishu-card-progress/handler.py`：处理 `agent:interim` 分支（旁白优先）+ finish 统计尾部（命中率 + 人民币成本）

## 更新 hermes 后的检查清单

1. `hermes update` 后 → `grep -n 'emit("agent:interim"' gateway/run.py` 确认补丁还在
2. 若丢失 → 按上面的 diff 手动恢复（或 `git stash pop`——2026-08-19 验证过 stash 能带全部补丁恢复）
3. 重启 gateway 后 → 跑一个 3+ 步任务，确认卡片显示 💬 旁白 + 统计尾部（命中率/成本）
