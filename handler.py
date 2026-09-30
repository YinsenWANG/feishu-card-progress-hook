#!/usr/bin/env python3
"""External gateway progress hook. No core patch or inferred telemetry."""
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

# Load only a sibling module, independent of gateway/plugin sys.path changes.
import importlib.util
_spec = importlib.util.spec_from_file_location("card_progress_storage", Path(__file__).with_name("storage.py"))
_storage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_storage)

SCRIPT = str(Path(__file__).resolve().with_name("card-stream.py"))
STALE_SECONDS = 30 * 60
MIN_CARD_STEP = 2  # Third iteration preparation; previous tool round is attached.
THROTTLE_SECONDS = 1.0
MAX_STEPS_SHOWN = 30
_logger = logging.getLogger("feishu-card-progress")


def log(msg):
    # Import has no filesystem effects, including when the logs directory is absent.
    _logger.info(msg)


def state_path():
    return _storage.home() / "cache" / "hook_card_state.json"


def load_state():
    return _storage.load(state_path())


def save_state(state):
    _storage.save(state_path(), state)


TOOL_FRIENDLY = {
    "terminal": "终端", "read_file": "读文件", "write_file": "写文件",
    "patch": "改文件", "search_files": "搜索", "web_search": "联网搜索",
    "web_fetch": "抓网页", "browser_exec": "浏览器", "session_search": "查历史",
    "skill_view": "读技能", "skills_list": "列技能", "skill_manage": "改技能",
    "memory": "记忆", "cronjob": "定时任务", "delegate_task": "子代理",
    "execute_code": "执行代码", "vision_analyze": "看图", "todo": "任务清单",
    "clarify": "询问", "text_to_speech": "语音", "lark-cli": "飞书操作",
    "gh": "GitHub", "git": "Git", "process": "进程", "tool_search": "查工具",
}

# CNY / 1M tokens: UNVERIFIED historical snapshots, not current provider rates.
MODEL_PRICING = {
    # 完整模型名（agent_result.model 通常是 provider/model 或裸名）
    "deepseek-v4-flash": {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_write": 0.2},
    "deepseek/deepseek-v4-flash": {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_write": 0.2},
    "deepseek-v4-pro": {"input": 3.2, "output": 6.4, "cache_read": 0.32, "cache_write": 0.64},
    "deepseek/deepseek-v4-pro": {"input": 3.2, "output": 6.4, "cache_read": 0.32, "cache_write": 0.64},
}


def format_token_stats(_in, _out, _cr, _cw):
    """格式化 token 统计：↑输入 ↓输出 ☁缓存命中率(缓存量)（dsh 方法）"""
    _in_s = f"{_in:,}"
    _out_s = f"{_out:,}"
    _prompt_total = _in + _cr + _cw
    if _cr > 0 and _prompt_total > 0:
        _pct = 100 * _cr / _prompt_total
        # 命中率 ≥95% 显示 ☁95%；否则显示缓存量
        return f"↑{_in_s} ↓{_out_s} ☁{_pct:.0f}%"
    if _cr > 0:
        return f"↑{_in_s} ↓{_out_s} ☁{_cr:,}"
    return f"↑{_in_s} ↓{_out_s}"


def estimate_turn_cost(model, _in, _out, _cr, _cw):
    """按精确注册模型名估算 CNY；表中数字仅为未经验证的历史快照。"""
    try:
        p = MODEL_PRICING.get(model)
        if not p:
            return None
        cost = (_in * p["input"] + _out * p["output"]
                + _cr * p["cache_read"] + _cw * p["cache_write"]) / 1_000_000
        return cost
    except Exception:
        return None

NARRATION = "\x01"  # 旁白内部标记（显示时去掉，用户 2026-08-14 不要 💬 emoji）

def is_narration(s):
    return isinstance(s, str) and s.startswith(NARRATION)


def clip(s, n):
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def clip_line(text, max_len=50):
    """旁白单行截断（2026-08-15 修复）：不产生换行符，超长加 …。
    飞书卡片 markdown 的 • 列表每行必须完整一行才有统一缩进；
    之前 clip_multiline 产生 \n 导致列表第二行失去 • 前缀，渲染混乱。
    max_len=50：移动端约 30-40 字符/行，50 字符内可接受，超长截断加 …。"""
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= max_len:
        return text
    # 在 0.8*max_len 附近找自然断点（标点/空格），找不到就硬切
    cut = int(max_len * 0.8)
    for pos in range(cut, max_len):
        if text[pos] in "，。！？；：、,.!?;: ":
            cut = pos
    return text[: cut + 1] + "…"


def parse_args(raw):
    """arguments 可能是 JSON 字符串或 dict，容错解析为 dict。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def summarize_terminal(cmd):
    """终端命令 → 语义摘要。

    v4 核心升级（用户反馈：命令式字符无语义）：
      1. echo "语义标记" → 直接用 echo 内容（agent 自己写的语义，天然可读）
      2. 常见命令模式映射成语义动作（git/grep/运行脚本/委派 agent/查看文件等）
      3. 兜底：程序名 + 关键参数
    """
    import re
    cmd = (cmd or "").strip()
    # 去掉开头的 cd xxx && （仅当确实是 cd 开头）
    m_cd = re.match(r'^cd\s+\S+\s*&&\s*', cmd)
    if m_cd:
        cmd = cmd[m_cd.end():].strip()
    if not cmd:
        return "终端"
    # 1. echo 语义标记：echo "=== 检查 CLI 状态 ===" → 检查 CLI 状态
    m = re.search(r'echo\s+["\'](.+?)["\']', cmd)
    if m:
        s = m.group(1).strip()
        s = re.sub(r'^[=\-*\s]+|[=\-*\s]+$', '', s)  # 去掉 === 装饰
        if s:
            return clip(s, 48)
    # 2. 常见命令模式
    # git 操作
    m = re.match(r'^\s*git\s+(\S+)\s*(.*)$', cmd)
    if m:
        sub = m.group(1)
        rest = clip(re.sub(r'^\s*-\S+\s*', '', m.group(2)).strip().split("&&")[0], 24)
        return f"Git {sub}" + (f" {rest}" if rest else "")
    # 委派 agent（本机 5 个 CLI）
    m = re.match(r'^\s*(dsh|codex|grok|agy|pi|claude)\b', cmd)
    if m:
        agent = {"dsh": "dsh", "codex": "Codex", "grok": "Grok", "agy": "Antigravity", "pi": "Pi", "claude": "Claude"}[m.group(1)]
        # 提取任务字符串（引号里的内容）
        task = re.search(r'["\'](.+?)["\']', cmd)
        task_s = clip(task.group(1), 36) if task else ""
        return f"委派 {agent}" + (f"：{task_s}" if task_s else "")
    # 运行 python 脚本
    m = re.match(r'^\s*python3?\s+(\S+\.py)', cmd)
    if m:
        return f"运行脚本 {m.group(1)}"
    # 查看文件
    m = re.match(r'^\s*(cat|less|head|tail|wc)\s+(\S+)', cmd)
    if m:
        return f"查看 {m.group(2)}"
    # 列出目录
    if re.match(r'^\s*ls\b', cmd):
        rest = re.sub(r'^\s*-\S+\s*', '', cmd[2:]).strip()
        return f"查看目录" + (f" {clip(rest, 24)}" if rest else "")
    # 搜索（去掉 -rn 等选项）
    m = re.match(r'^\s*(grep|rg)\s+(.+)$', cmd)
    if m:
        pat = re.sub(r'^\s*-\S+\s*', '', m.group(2)).strip()
        return f"搜索 {clip(pat, 36)}"
    # 飞书操作
    if re.match(r'^\s*lark-cli\b', cmd):
        sub = re.search(r'lark-cli\s+(\S+)\s+(\S+)', cmd)
        if sub:
            return f"飞书 {sub.group(1)}/{sub.group(2)}"
        return "飞书操作"
    # 3. 兜底：程序名
    prog = cmd.split()[0].replace("/", " ").split()[-1] if cmd.split() else "终端"
    prog_map = {"python3": "Python", "node": "Node", "npm": "npm", "brew": "brew",
                "sleep": "等待", "ps": "查进程", "cp": "复制", "mv": "移动",
                "mkdir": "建目录", "rm": "删除", "find": "查找文件", "echo": "输出",
                "cat": "查看", "uname": "查系统", "sw_vers": "查系统", "date": "查时间"}
    friendly_prog = prog_map.get(prog, prog)
    rest = clip(cmd[len(prog):].strip(), 30)
    return f"{friendly_prog} {rest}" if rest else f"{friendly_prog}"


def summarize(name, args):
    """从工具名 + 参数生成一步的语义摘要（如：联网搜索 DeepSeek V4）。"""
    if not name:
        return ""
    # 按工具类型提取关键参数
    if name == "terminal":
        cmd = args.get("command") or args.get("cmd") or ""
        return summarize_terminal(cmd)
    if name in ("read_file", "write_file", "patch"):
        p = args.get("path") or ""
        if name == "write_file":
            return f"写入 {clip(p, 48)}"
        if name == "patch":
            return f"编辑 {clip(p, 48)}"
        return f"读取 {clip(p, 48)}"
    if name == "search_files":
        pat = args.get("pattern") or ""
        p = args.get("path") or ""
        return f"搜索 {clip(pat, 24)}" + (f" @{clip(p, 20)}" if p else "")
    if name == "web_search":
        q = args.get("query") or ""
        return f"联网搜索：{clip(q, 44)}"
    if name == "web_fetch":
        u = args.get("url") or ""
        return f"抓取 {clip(u, 44)}"
    if name == "skill_view":
        s = args.get("name") or ""
        return f"读技能 {clip(s, 30)}"
    if name == "skill_manage":
        s = args.get("name") or ""
        act = args.get("action") or ""
        return f"{act or '管理'}技能 {clip(s, 24)}"
    if name == "skills_list":
        return "列出技能"
    if name == "delegate_task":
        g = args.get("goal") or ""
        return f"子代理：{clip(g, 44)}"
    if name == "execute_code":
        return "执行代码"
    if name == "memory":
        act = args.get("action") or ""
        tgt = args.get("target") or ""
        return f"记忆（{act}{'/' + tgt if tgt else ''}）"
    if name == "cronjob":
        act = args.get("action") or ""
        return f"定时任务（{act}）"
    if name == "vision_analyze":
        u = args.get("image_url") or ""
        return f"看图 {clip(u, 40)}"
    if name == "clarify":
        q = args.get("question") or ""
        return f"询问：{clip(q, 36)}"
    if name == "text_to_speech":
        t = args.get("text") or ""
        return f"语音：{clip(t, 30)}"
    if name == "todo":
        act = args.get("action") or ""
        return f"任务清单（{act}）"
    if name == "process":
        act = args.get("action") or ""
        return f"进程（{act}）"
    if name == "lark-cli":
        return "飞书操作"
    # 兜底：参数 JSON 前 40 字符
    if args:
        raw = json.dumps(args, ensure_ascii=False)
        return f"{TOOL_FRIENDLY.get(name, name)}：{clip(raw, 40)}"
    return TOOL_FRIENDLY.get(name, name) or name


def build_content(task, steps, total_tools):
    """卡片内容：步骤摘要列表（最近 MAX_STEPS_SHOWN 步）。
    2026-08-14 优化：
      - 去掉任务名（📋 原始指令）行
      - 去掉"执行摘要"标题
      - 去掉底部统计（finish 时追加）
      - 旁白不显示 💬 emoji（内部 NARRATION 标记，显示时去掉）
      - 换行用 markdown 列表语法（• 前缀），不用 <br>"""
    lines = []
    if steps:
        shown = steps[-MAX_STEPS_SHOWN:]
        hidden = len(steps) - len(shown)
        if hidden > 0:
            lines.append(f"… 前 {hidden} 步已折叠")
        for s in shown:
            s_clean = str(s)
            if s_clean.startswith(NARRATION):
                s_clean = s_clean[len(NARRATION):]
            # 2026-08-15：用飞书官方 markdown 无序列表语法 `- `（比 `• ` 兼容性更广）
            lines.append(f"- {s_clean}")
    return "\n".join(lines)


def run_script(args, timeout=30, retries=2):
    # Sending is non-idempotent: a timeout may follow remote acceptance. Never retry send.
    attempts = 1 if args[0] == "send" else retries + 1
    last = (-1, "", "")
    for attempt in range(attempts):
        try:
            r = subprocess.run([sys.executable, SCRIPT] + args,
                               capture_output=True, text=True, timeout=timeout,
                               env={**os.environ, "HERMES_HOME": str(_storage.home())})
            last = (r.returncode, r.stdout.strip(), r.stderr.strip())
            if r.returncode == 0:
                return last
        except (OSError, subprocess.SubprocessError) as exc:
            last = (-1, "", type(exc).__name__)
        if attempt + 1 < attempts:
            time.sleep(1.0)
    log(f"card operation failed action={args[0]} rc={last[0]}")
    return last


def _body(rec):
    return build_content("", rec.get("steps", []), rec.get("tool_count", 0))


def _terminal(rec, title, detail):
    rec["phase"] = "terminal"
    rec["terminal_title"] = title
    duration = max(0, int(time.time() - rec["started"]))
    stats = f"⏱️ {duration}s · token 未知 · 缓存未知 · 成本未知 · API 调用未知"
    rec["terminal_content"] = "\n\n".join(p for p in (_body(rec), detail, stats) if p)


def _finish(rec):
    if not rec.get("message_id"):
        return True
    rc, _, _ = run_script(["finish", rec["message_id"], rec["terminal_title"], rec["terminal_content"]])
    return rc == 0


def cleanup_stale(state):
    now = time.time()
    for sid, rec in list(state.items()):
        if rec.get("phase") != "terminal" and now - rec.get("last_activity", rec["started"]) > STALE_SECONDS:
            _terminal(rec, "⚠️ 任务中断", "长时间无更新，自动标记中断")
        if rec.get("phase") == "terminal" and _finish(rec):
            state.pop(sid, None)


def _refresh(rec, state, allow_send=False):
    now = time.time()
    mid = rec.get("message_id")
    if mid and now - rec.get("last_update", 0) < THROTTLE_SECONDS:
        return
    title = "🔄 任务处理中"
    if mid:
        rc, _, _ = run_script(["replace", mid, title, _body(rec)])
        if rc == 0:
            rec["last_update"] = now
    elif allow_send and not rec.get("send_attempted"):
        rec["send_attempted"] = True
        save_state(state)  # Persist before a potentially accepted send/timeout/crash.
        rc, out, _ = run_script(["send", rec["chat_id"], title, _body(rec)])
        mid = (out.splitlines()[-1] if out else "").strip()
        if rc == 0 and mid.startswith("om_"):
            rec["message_id"] = mid
            rec["last_update"] = now


def handle(event_type, context):
    """Native gateway entry point; observer failures never escape into the gateway."""
    if event_type not in {"agent:start", "agent:step", "agent:end"}:
        return None
    return _dispatch(event_type, context or {})


def observe(event_type, context):
    """Internal adapter for the two verified plugin observers; no synthetic gateway event."""
    if event_type not in {"bind_turn", "narration"}:
        return None
    return _dispatch(event_type, context)


def _dispatch(event_type, ctx):
    if not isinstance(ctx, dict):
        return None
    if ctx.get("platform") != "feishu":
        return None
    sid = ctx.get("session_id")
    if not isinstance(sid, str) or not sid or sid.startswith(("cron", "deleg")):
        return None
    try:
        with _storage.lock(state_path()):
            state = load_state()
            cleanup_stale(state)
            _handle(event_type, ctx, state, sid)
            save_state(state)
    except Exception as exc:
        log(f"handler error event={event_type} type={type(exc).__name__}")
    return None


def _handle(event_type, ctx, state, sid):
    rec = state.get(sid)
    if event_type == "agent:start":
        # Reject groups AND unknown chat types; never infer a DM from its identifier.
        if rec:
            if rec.get("phase") != "terminal":
                _terminal(rec, "⚠️ 任务中断", "新任务开始，旧任务已中断")
            if not _finish(rec):
                state["pending:" + uuid.uuid4().hex] = rec
            state.pop(sid, None)
        if ctx.get("chat_type") != "dm" or not ctx.get("chat_id"):
            return
        state[sid] = {
            "chat_id": ctx["chat_id"], "chat_type": "dm", "message_id": None,
            "steps": [], "tool_count": 0, "started": time.time(),
            "last_activity": time.time(), "last_update": 0, "phase": "running",
            "turn_id": None, "narrated_iterations": [], "step_iterations": [],
        }
        return
    if not rec or rec.get("phase") != "running" or rec.get("chat_type") != "dm":
        return
    if "chat_type" in ctx and ctx["chat_type"] != "dm":
        return
    if event_type in {"bind_turn", "narration"}:
        turn_id = ctx.get("turn_id")
        if not turn_id:
            return
        if event_type == "bind_turn":
            # Bind once per gateway start; queued narration from earlier turns cannot rebind.
            if rec["turn_id"] is None:
                rec["turn_id"] = turn_id
            return
        if rec["turn_id"] != turn_id:
            return
        text = str(ctx.get("text") or "").strip()
        if not text:
            return
        iteration = int(ctx.get("iteration", 0))
        # Replace fallbacks only for this native iteration (commentary can precede tools).
        entries = rec.setdefault("step_iterations", [])
        steps = rec["steps"]
        for index in range(len(steps) - 1, -1, -1):
            if entries[index] == iteration and not is_narration(steps[index]):
                del steps[index]
                del entries[index]
        steps.append(NARRATION + clip_line(text))
        entries.append(iteration)
        if iteration not in rec["narrated_iterations"]:
            rec["narrated_iterations"].append(iteration)
        rec["last_activity"] = time.time()
        _refresh(rec, state)
        return
    if event_type == "agent:step":
        iteration = int(ctx.get("iteration", 0) or 0)
        tools = ctx.get("tools") or ctx.get("tool_names") or []
        rec["tool_count"] += len(tools)
        if iteration not in rec["narrated_iterations"]:
            for tool in tools:
                name = tool.get("name", "") if isinstance(tool, dict) else str(tool)
                args = parse_args(tool.get("arguments")) if isinstance(tool, dict) else {}
                summary = summarize(name, args)
                if summary:
                    rec["steps"].append(summary)
                    rec["step_iterations"].append(iteration)
        rec["last_activity"] = time.time()
        _refresh(rec, state, allow_send=iteration >= MIN_CARD_STEP)
    elif event_type == "agent:end":
        # Native end precedes delivery and carries neither outcome nor usage.
        # Response text is never copied or parsed to guess success/failure.
        _terminal(rec, "⚠️ 处理结束 · 投递状态未知", "原生事件未提供任务结果与投递状态")
        if _finish(rec):
            state.pop(sid, None)


if __name__ == "__main__":
    if len(sys.argv) == 3:
        handle(sys.argv[1], json.loads(sys.argv[2]))
