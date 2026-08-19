#!/usr/bin/env python3
"""飞书卡片流式更新工具（CardKit 版）
用法:
  card-stream.py send <chat_id> <标题> [<内容>]        # 发卡片, 输出 message_id
  card-stream.py update <message_id> <标题> [<内容>]    # 流式更新（默认追加模式）
  card-stream.py replace <message_id> <标题> [<内容>]   # 替换模式（覆盖旧内容）
  card-stream.py finish <message_id> <标题> <内容>      # 完成态（绿色 + 追加）

默认追加模式：新内容追加到卡片已有内容后面（保留历史），更适合过程展示。

身份：所有 lark-cli 调用使用环境变量 LARK_CLI_PROFILE 指定的 profile；
未设置时使用 lark-cli 全局默认配置。请确保调用环境已正确配置飞书应用凭据。
"""
import sys
import json
import subprocess
import os
import uuid
import re

CACHE = os.path.expanduser("~/.hermes/cache/card_stream_state.json")
ELEMENT_ID = "md_1"
# 2026-08-15 加固：缓存条目上限（防止无限增长），超出时保留最近 N 条
MAX_CACHE_ENTRIES = 60

def load_cache():
    try:
        with open(CACHE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_cache(c):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    # 2026-08-15 加固：清理超限条目（dict 保持插入顺序，保留最近 MAX_CACHE_ENTRIES 条）
    if len(c) > MAX_CACHE_ENTRIES:
        # 旧条目在 dict 前面，删掉最旧的 (len - MAX) 条
        excess = len(c) - MAX_CACHE_ENTRIES
        for k in list(c.keys())[:excess]:
            c.pop(k, None)
    with open(CACHE, "w") as f:
        json.dump(c, f, ensure_ascii=False, indent=1)

def run_cmd(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    raw = r.stdout
    idx = raw.find("{")
    try:
        return json.loads(raw[idx:])
    except Exception:
        return {"ok": False, "error": {"message": raw[:200]}}

def norm(content):
    """规范化内容：
    1. 字面 \\n（反斜杠+n）→ 真实换行
    2. checkbox 语法 → emoji 状态展示（- [x] → ✅，- [ ] → ⬜）
    3. 单换行保留（飞书 markdown 支持 \n 换行，2026-08-14 修复：不再转 <br>）
    """
    if not isinstance(content, str):
        return content
    # 字面反斜杠+n → 真实换行
    content = content.replace("\\n", "\n")
    # checkbox → emoji 状态（注意顺序：先 [x] 后 [ ]）
    lines = content.split("\n")
    new_lines = []
    for line in lines:
        # - [x] 已完成 → ✅ 已完成
        if re.match(r'^\s*[-*]\s*\[[xX]\]\s*', line):
            line = re.sub(r'^\s*[-*]\s*\[[xX]\]\s*', '✅ ', line)
        # - [ ] 待做 → ⬜ 待做
        elif re.match(r'^\s*[-*]\s*\[\s*\]\s*', line):
            line = re.sub(r'^\s*[-*]\s*\[\s*\]\s*', '⬜ ', line)
        # 进行中 - [~] → 🔄（可选）
        elif re.match(r'^\s*[-*]\s*\[[~]\s*\]\s*', line):
            line = re.sub(r'^\s*[-*]\s*\[[~]\s*\]\s*', '🔄 ', line)
        new_lines.append(line)
    # 保留真实换行（飞书 markdown 渲染 \n）
    content = "\n".join(new_lines)
    return content

def build_card(title, content, template="blue"):
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "body": {"elements": [{"tag": "markdown", "element_id": ELEMENT_ID, "content": norm(content)}]},
    }

def get_state(mid):
    cache = load_cache()
    st = cache.get(mid)
    if not st or not st.get("card_id"):
        conv = run_cmd(["lark-cli", *(["--profile", os.environ["LARK_CLI_PROFILE"]] if os.environ.get("LARK_CLI_PROFILE") else []), "api", "POST", "/open-apis/cardkit/v1/cards/id_convert",
                        "--data", json.dumps({"message_id": mid}),
                        "--as", "bot", "--format", "json"])
        cid = ""
        if conv.get("ok") or conv.get("code") == 0:
            cid = conv.get("data", {}).get("card_id", "") or ""
        st = {"card_id": cid, "seq": 0, "content": ""}
        cache[mid] = st
        save_cache(cache)
    return st

def bump_seq(mid, st):
    st["seq"] = st.get("seq", 0) + 1
    cache = load_cache()
    cache[mid] = st
    save_cache(cache)
    return st["seq"]

def cardkit_update(mid, content, append=True, header=None):
    """CardKit 更新。成功 True，不可用/失败 None。"""
    st = get_state(mid)
    cid = st.get("card_id", "")
    if not cid:
        return None
    if append:
        prev = st.get("content", "") or ""
        if prev:
            content = prev + "\n" + content
        st["content"] = content
    else:
        st["content"] = content
    seq = bump_seq(mid, st)
    actions = [{"action": "partial_update_element",
                "params": {"element_id": ELEMENT_ID, "partial_element": {"content": content}}}]
    if header:
        actions.append({"action": "partial_update_setting",
                        "params": {"settings": {"header": {"template": header}}}})
    body = {"uuid": str(uuid.uuid4()), "sequence": seq,
            "actions": json.dumps(actions, ensure_ascii=False)}
    d = run_cmd(["lark-cli", *(["--profile", os.environ["LARK_CLI_PROFILE"]] if os.environ.get("LARK_CLI_PROFILE") else []), "api", "POST", f"/open-apis/cardkit/v1/cards/{cid}/batch_update",
                 "--data", json.dumps(body, ensure_ascii=False),
                 "--as", "bot", "--format", "json"])
    if d.get("ok") or d.get("code") == 0:
        cache = load_cache()
        cache[mid] = st
        save_cache(cache)
        return True
    return None

def im_patch(mid, title, content, template="blue"):
    """im PATCH 整卡更新，带 3 次重试 + 指数退避。成功 True，失败 False。"""
    card = build_card(title, content, template)
    content_json = json.dumps(card, ensure_ascii=False)
    payload = json.dumps({"msg_type": "interactive", "content": content_json}, ensure_ascii=False)
    import time as _time
    for attempt in range(3):
        d = run_cmd(["lark-cli", *(["--profile", os.environ["LARK_CLI_PROFILE"]] if os.environ.get("LARK_CLI_PROFILE") else []), "api", "PATCH", f"/open-apis/im/v1/messages/{mid}",
                     "--data", payload, "--as", "bot", "--format", "json"])
        if d.get("ok") or d.get("code") == 0:
            return True
        if attempt < 2:
            _time.sleep(1.5 * (attempt + 1))  # 1.5s, 3s 退避
    return False

def send(chat_id, title, content=""):
    card = build_card(title, content or "正在处理...", "blue")
    payload = json.dumps(card, ensure_ascii=False)
    d = run_cmd(["lark-cli", *(["--profile", os.environ["LARK_CLI_PROFILE"]] if os.environ.get("LARK_CLI_PROFILE") else []), "im", "+messages-send", "--chat-id", chat_id,
                 "--msg-type", "interactive", "--content", payload, "--as", "bot", "--format", "json"])
    if not (d.get("ok") or d.get("code") == 0):
        print(json.dumps(d.get("error", d), ensure_ascii=False), file=sys.stderr)
        return 1
    mid = d.get("data", {}).get("message_id")
    if not mid:
        print("no message_id in response", file=sys.stderr)
        return 1
    conv = run_cmd(["lark-cli", *(["--profile", os.environ["LARK_CLI_PROFILE"]] if os.environ.get("LARK_CLI_PROFILE") else []), "api", "POST", "/open-apis/cardkit/v1/cards/id_convert",
                    "--data", json.dumps({"message_id": mid}),
                    "--as", "bot", "--format", "json"])
    cid = ""
    if conv.get("ok") or conv.get("code") == 0:
        cid = conv.get("data", {}).get("card_id", "") or ""
    cache = load_cache()
    cache[mid] = {"card_id": cid, "seq": 0, "content": norm(content or "正在处理..."), "chat_id": chat_id}
    save_cache(cache)
    print(mid)
    return 0

def update(mid, title, content="", append=True):
    # 2026-08-13 定稿：原生 im PATCH 主推（简单可靠），CardKit 仅作回退
    if im_patch(mid, title, content):
        return 0
    r = cardkit_update(mid, content, append=append)
    if r is True:
        return 0
    # 终极兜底：卡片全挂时用 hermes send 发纯文本（零 LLM，保证用户看到进度）
    st = get_state(mid)
    chat_id = st.get("chat_id", "") if st else ""
    if chat_id and fallback_text(chat_id, title, content):
        print(f"update failed for {mid}, fell back to text", file=sys.stderr)
        return 0
    print(f"update failed for {mid}", file=sys.stderr)
    return 1

def finish(mid, title, content):
    # 2026-08-13 定稿：原生 im PATCH 主推（简单可靠），CardKit 仅作回退
    if im_patch(mid, title, content, "green"):
        return 0
    r = cardkit_update(mid, content, append=True, header="green")
    if r is True:
        return 0
    # 终极兜底：卡片全挂时用 hermes send 发纯文本
    st = get_state(mid)
    chat_id = st.get("chat_id", "") if st else ""
    if chat_id and fallback_text(chat_id, title, content):
        print(f"finish failed for {mid}, fell back to text", file=sys.stderr)
        return 0
    print(f"finish failed for {mid}", file=sys.stderr)
    return 1

def fallback_text(chat_id, title, content):
    """终极兜底：卡片全挂时用 hermes send 发纯文本（零 LLM，保证用户看到进度）"""
    import subprocess as _sp
    text = f"{title}\n{content}"
    try:
        r = _sp.run(["hermes", "send", "-t", f"feishu:{chat_id}", text],
                    capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False

def build_progress_content(steps_done, steps_total, step_desc=""):
    """渲染步骤进度：✅ 已完成 / 🔄 当前 / ⬜ 待做 + 进度 emoji"""
    bar_len = 12
    done = int(steps_done / steps_total * bar_len) if steps_total else 0
    bar = "█" * done + "░" * (bar_len - done)
    lines = [f"进度 {steps_done}/{steps_total}  {bar}"]
    if step_desc:
        lines.append(step_desc)
    return "\n".join(lines)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    action = sys.argv[1]
    if action == "send":
        sys.exit(send(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "", sys.argv[4] if len(sys.argv) > 4 else ""))
    elif action == "update":
        sys.exit(update(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "", sys.argv[4] if len(sys.argv) > 4 else "", True))
    elif action == "replace":
        sys.exit(update(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "", sys.argv[4] if len(sys.argv) > 4 else "", False))
    elif action == "finish":
        sys.exit(finish(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "", sys.argv[4] if len(sys.argv) > 4 else ""))
    elif action == "progress":
        # progress <message_id> <标题> <done> <total> [步骤描述]
        mid = sys.argv[2]
        title = sys.argv[3] if len(sys.argv) > 3 else ""
        done = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        total = int(sys.argv[5]) if len(sys.argv) > 5 else 1
        desc = sys.argv[6] if len(sys.argv) > 6 else ""
        content = build_progress_content(done, total, desc)
        sys.exit(update(mid, title, content, False))
    else:
        print(f"未知动作: {action}")
        sys.exit(1)
