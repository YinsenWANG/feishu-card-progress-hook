#!/usr/bin/env python3
"""
自动进度卡片 Hook — v3（步骤摘要版）
为飞书长任务自动管理交互卡片（agent:start/step/end 事件驱动）。

v3 相对 v2 的核心升级（2026-08-14 用户反馈）：
  v2 只显示工具名（"终端 ×7"），没有信息量。用户要的是【过程步骤摘要】——
  每步"在干什么"（如：联网搜索 DeepSeek V4 / 抓取 arxiv 论文 / 写入 research.md）。
  数据来源：agent:step 的 tools 字段（conversation_loop 已构建 {name, result, arguments}），
  之前 handler 只读了 tool_names，白白丢弃了 arguments。

设计约束（保持 v2）：
  - 只处理 feishu；跳过 cron / deleg 会话
  - iteration < 2 不建卡；更新节流 1s
  - agent:end 必 finish；30min 僵尸惰性清理
  - finish 不写 response（防敏感泄漏）；失败写日志
"""
import json
import logging
import os
import re
import subprocess
import time

CACHE = os.path.expanduser("~/.hermes/cache/hook_card_state.json")
SCRIPT = os.path.expanduser("~/.hermes/scripts/card-stream.py")
LOG_FILE = os.path.expanduser("~/.hermes/logs/hook_card_progress.log")
STALE_SECONDS = 30 * 60
MIN_CARD_STEP = 2
THROTTLE_SECONDS = 1.0
MAX_STEPS_SHOWN = 30   # 卡片最多保留最近 30 步摘要，超出折叠
TASK_TITLE_MAX = 16    # 卡片标题里的任务名长度

# 工具名 → 友好名
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

# 模型定价表（CNY / 1M tokens，来源：DeepSeek 官方人民币价目 + Hermes 定价快照换算）
# 缓存命中率 = cache_read / (input + cache_read + cache_write)（dsh 方法）
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
    """估算本轮成本（USD）——缓存命中按折扣价计（dsh 的成本感知方法）"""
    try:
        p = MODEL_PRICING.get(model) or MODEL_PRICING.get(model.split("/")[-1])
        if not p:
            # 带日期后缀的模型（deepseek-v4-flash-0731）做前缀匹配
            _short = model.split("/")[-1]
            for _key, _val in MODEL_PRICING.items():
                if _key.startswith(_short.rsplit("-", 1)[0]) or _short.startswith(_key.split("/")[-1]):
                    p = _val
                    break
        if not p:
            return None
        cost = (_in * p["input"] + _out * p["output"]
                + _cr * p["cache_read"] + _cw * p["cache_write"]) / 1_000_000
        return cost
    except Exception:
        return None

_logger = logging.getLogger("feishu-card-progress")
if not _logger.handlers:
    _handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def log(msg):
    _logger.info(msg)


def load_state():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE)


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
    last = (-1, "", "")
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(["python3", SCRIPT] + args,
                               capture_output=True, text=True, timeout=timeout)
            last = (r.returncode, r.stdout.strip(), r.stderr.strip())
            if r.returncode == 0:
                return last
        except Exception as e:
            last = (-1, "", str(e))
        if attempt < retries:
            time.sleep(1.0)
    return last


def finish_card(rec, title, content):
    mid = rec.get("message_id")
    if not mid:
        return
    rc, out, err = run_script(["finish", mid, title, content])
    if rc != 0:
        log(f"finish failed mid={mid} rc={rc} err={err[:200]}")


def cleanup_stale(state):
    now = time.time()
    changed = False
    for sid, rec in list(state.items()):
        age = now - max(rec.get("started", 0), rec.get("last_update", 0))
        if age > STALE_SECONDS:
            if rec.get("message_id"):
                finish_card(rec, "⚠️ 任务中断", "长时间无更新，自动标记中断")
                log(f"stale session finished: {sid} age={int(age)}s")
            state.pop(sid, None)
            changed = True
    if changed:
        save_state(state)


def handle(event_type, context):
    try:
        return _handle(event_type, context or {})
    except Exception as e:
        log(f"handler error {event_type}: {e}")
        return None


def _handle(event_type, ctx):
    if ctx.get("platform", "") != "feishu":
        return None

    session_id = ctx.get("session_id", "")
    if not session_id:
        return None
    if session_id.startswith("cron") or session_id.startswith("deleg"):
        return None

    state = load_state()
    cleanup_stale(state)
    rec = state.get(session_id)

    if event_type == "agent:start":
        chat_id = ctx.get("chat_id", "")
        if not chat_id:
            return None
        # 2026-08-15 诊断：记录重置前的旧状态（排查"复用旧卡"）
        old_mid = (state.get(session_id) or {}).get("message_id")
        state[session_id] = {
            "chat_id": chat_id,
            "message_id": None,
            "tools": [],
            "steps": [],
            "task": clip(ctx.get("message") or "", 44),
            "started": time.time(),
            "last_update": 0.0,
            "last_interim_time": 0.0,
        }
        save_state(state)
        log(f"agent:start reset session={session_id} old_mid={old_mid}")
        return None

    if not rec:
        return None

    if event_type == "agent:interim":
        # 模型旁白 → 作为步骤摘要（用户 2026-08-14：卡片步骤用旁白而非命令反推）
        # P1: 旁白优先——若末尾有连续的命令摘要（非旁白），全部替换成这一条旁白
        #     （一个 step 事件可能带多个工具 → 多条命令摘要，interim 应整体替换）
        # P2: 1s 节流内连续旁白合并为一条（"…"连接）
        # P3: 按行截断，避免长旁白撑爆卡片
        # 2026-08-14: 旁白用 NARRATION 标记（不显示 emoji，build_content 去掉标记）
        text = str(ctx.get("text") or "").strip()
        if not text:
            return None
        now = time.time()
        steps = rec.setdefault("steps", [])
        # 找到末尾连续非旁白条目的起点（整体替换）
        cut = len(steps)
        while cut > 0 and not is_narration(steps[cut - 1]):
            cut -= 1
        is_cmd_fallback = cut < len(steps)
        if is_cmd_fallback:
            # 替换掉末尾全部命令反推摘要（旁白优先）
            del steps[cut:]
        # 2026-08-15：每条旁白独立成行（不合并）——合并导致 "… …" 难看且行过长
        steps.append(f"{NARRATION}{clip_line(text)}")
        rec["last_interim_time"] = now
        if rec.get("message_id") and now - rec.get("last_update", 0) < THROTTLE_SECONDS:
            save_state(state)
            return None
        chat_id = rec.get("chat_id", "")
        if not chat_id:
            save_state(state)
            return None
        title = "🔄 任务处理中"
        content = build_content(rec.get("task", ""), rec.get("steps", []), len(rec.get("tools", [])))
        if rec.get("message_id"):
            mid = rec["message_id"]
            rc, out, err = run_script(["replace", mid, title, content])
            if rc == 0:
                rec["last_update"] = now
                log(f"INTERIM replace session={session_id} mid={mid}")
        # 无论是否已建卡，都要保存（卡片未创建时步骤仍要落盘）
        save_state(state)
        return None

    if event_type == "agent:step":
        iteration = int(ctx.get("iteration", 0) or 0)
        tools = ctx.get("tools") or ctx.get("tool_names") or []
        # 记录工具名（计数用），但步骤摘要优先用旁白（agent:interim）。
        # 仅当该轮还没有旁白步骤时才用命令反推摘要兜底，避免重复刷屏。
        for t in tools:
            if isinstance(t, dict):
                name = t.get("name") or ""
                rec.setdefault("tools", []).append(name)
            else:
                rec.setdefault("tools", []).append(str(t))
        # 始终追加命令摘要（兜底），后续 interim 到达时会替换掉它（旁白优先）。
        # 真实顺序：step（工具调用）→ interim（模型旁白），interim 负责替换。
        for t in tools:
            if isinstance(t, dict):
                name = t.get("name") or ""
                args = parse_args(t.get("arguments"))
                rec.setdefault("steps", []).append(summarize(name, args))

        if iteration < MIN_CARD_STEP:
            save_state(state)
            return None

        now = time.time()
        if rec.get("message_id") and now - rec.get("last_update", 0) < THROTTLE_SECONDS:
            save_state(state)
            return None

        chat_id = rec.get("chat_id", "")
        if not chat_id:
            return None

        title = "🔄 任务处理中"
        content = build_content(rec.get("task", ""), rec.get("steps", []), len(rec.get("tools", [])))

        if not rec.get("message_id"):
            rc, out, err = run_script(["send", chat_id, title, content])
            mid = (out.splitlines()[-1] if out else "").strip()
            if mid.startswith("om_"):
                rec["message_id"] = mid
                rec["last_update"] = now
                save_state(state)
                log(f"SEND new card session={session_id} mid={mid}")
        else:
            mid = rec["message_id"]
            rc, out, err = run_script(["replace", mid, title, content])
            if rc == 0:
                rec["last_update"] = now
                save_state(state)
                log(f"REPLACE existing card session={session_id} mid={mid}")
        return None

    if event_type == "agent:end":
        mid = rec.get("message_id")
        tools_count = len(rec.get("tools", []))
        steps_count = len(rec.get("steps", []))
        duration = int(time.time() - rec.get("started", time.time()))
        # 诊断日志（2026-08-14：确认 end 事件是否携带 token 统计）
        log(f"agent:end ctx keys={sorted(ctx.keys())} tokens={ctx.get('tokens')} turn_seconds={ctx.get('turn_seconds')} api_calls={ctx.get('api_calls')}")
        if mid:
            title = "✅ 任务完成"
            body = build_content(rec.get("task", ""), rec.get("steps", []), tools_count)
            # 统计尾部（2026-08-15 用户要求：拆输入/输出 + 缓存命中，简略显示）
            _tokens = int(ctx.get("tokens") or 0)
            _in = int(ctx.get("input_tokens") or 0)
            _out = int(ctx.get("output_tokens") or 0)
            _cr = int(ctx.get("cache_read_tokens") or 0)
            _cw = int(ctx.get("cache_write_tokens") or 0)
            log(f"agent:end values in={_in} out={_out} cr={_cr} cw={_cw} tokens={_tokens}")
            _dur = int(ctx.get("turn_seconds") or duration)
            _api = int(ctx.get("api_calls") or tools_count)
            _stats = []
            if _dur > 0:
                _stats.append(f"⏱️ {_dur}s")
            if _in > 0 or _out > 0:
                # 输入/输出拆分 + 缓存命中率（dsh 方法 2026-08-19）
                _stats.append(format_token_stats(_in, _out, _cr, int(ctx.get("cache_write_tokens") or 0)))
                # 成本估算（缓存命中按折扣价计，dsh 成本感知方法；人民币显示）
                _cost = estimate_turn_cost(str(ctx.get("model") or ""), _in, _out, _cr, int(ctx.get("cache_write_tokens") or 0))
                if _cost is not None and _cost >= 0.0001:
                    _stats.append(f"¥{_cost:.4f}")
            elif _tokens > 0:
                _stats.append(f"{_tokens:,} tokens")
            _stats.append(f"{_api} 次调用")
            log(f"agent:end stats=[{', '.join(_stats)}] model={ctx.get('model')} in={_in} out={_out} cr={_cr} cw={ctx.get('cache_write_tokens')}")
            if body:
                content = f"{body}\n\n{(' · '.join(_stats))}"
            else:
                content = " · ".join(_stats)
            run_script(["finish", mid, title, content])
        state.pop(session_id, None)
        save_state(state)
        return None

    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        ev = sys.argv[1]
        c = json.loads(sys.argv[2])
        handle(ev, c)
        print("done")
