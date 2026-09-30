#!/usr/bin/env python3
"""飞书卡片流式更新工具（CardKit 版）
用法:
  card-stream.py send <chat_id> <标题> [<内容>]        # 发卡片, 输出 message_id
  card-stream.py update <message_id> <标题> [<内容>]    # 流式更新（默认追加模式）
  card-stream.py replace <message_id> <标题> [<内容>]   # 替换模式（覆盖旧内容）
  card-stream.py finish <message_id> <标题> <内容>      # 终态（覆盖，成功绿色；警告黄色）

默认追加模式：新内容追加到卡片已有内容后面（保留历史），更适合过程展示。

身份：所有 lark-cli 调用显式固定 --profile cli_a93011294a39dbc6。
状态使用活动 HERMES_HOME；失败不发送额外聊天消息。
"""
import sys
import json
import subprocess
import os
import uuid
import re

from pathlib import Path
from functools import wraps
import importlib.util
_spec = importlib.util.spec_from_file_location("card_stream_storage", Path(__file__).with_name("storage.py"))
_storage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_storage)

PROFILE = "cli_a93011294a39dbc6"
ELEMENT_ID = "md_1"
MAX_CACHE_ENTRIES = 60


def cache_path():
    return _storage.home() / "cache" / "card_stream_state.json"


def load_cache():
    return _storage.load(cache_path())


def save_cache(cache):
    for key in list(cache)[:max(0, len(cache) - MAX_CACHE_ENTRIES)]:
        cache.pop(key, None)
    _storage.save(cache_path(), cache)


def serialized(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        try:
            with _storage.lock(cache_path()):
                return fn(*args, **kwargs)
        except (OSError, ValueError, TimeoutError):
            print("card operation failed; state retained", file=sys.stderr)
            return 1
    return call


def run_cmd(cmd):
    # Every network boundary must use the explicit allowed profile, even new call sites.
    if cmd[:3] != ["lark-cli", "--profile", PROFILE]:
        raise ValueError("explicit card CLI profile required")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        raw = r.stdout
        idx = raw.find("{")
        data = json.loads(raw[idx:]) if idx >= 0 else {}
        if r.returncode == 0 and isinstance(data, dict):
            return data
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return {"ok": False}


def succeeded(data):
    return data.get("ok") is True or data.get("code") == 0

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
        conv = run_cmd(["lark-cli", "--profile", PROFILE, "api", "POST", "/open-apis/cardkit/v1/cards/id_convert",
                        "--data", json.dumps({"message_id": mid}),
                        "--as", "bot", "--format", "json"])
        cid = ""
        if succeeded(conv):
            cid = conv.get("data", {}).get("card_id", "") or ""
        st = {**(st or {}), "card_id": cid, "seq": (st or {}).get("seq", 0)}
        cache[mid] = st
        save_cache(cache)
    return st

def bump_seq(mid, st):
    st["seq"] = st.get("seq", 0) + 1
    cache = load_cache()
    cache[mid] = st
    save_cache(cache)
    return st["seq"]

def cardkit_update(mid, content, append=True, header=None, title=""):
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
                        "params": {"settings": {"header": {"template": header, "title": {"tag": "plain_text", "content": title}}}}})
    body = {"uuid": str(uuid.uuid4()), "sequence": seq,
            "actions": json.dumps(actions, ensure_ascii=False)}
    d = run_cmd(["lark-cli", "--profile", PROFILE, "api", "POST", f"/open-apis/cardkit/v1/cards/{cid}/batch_update",
                 "--data", json.dumps(body, ensure_ascii=False),
                 "--as", "bot", "--format", "json"])
    if succeeded(d):
        cache = load_cache()
        cache[mid] = st
        save_cache(cache)
        return True
    return None

def im_patch(mid, title, content, template="blue"):
    """Idempotent whole-card replacement. Handler owns bounded retries."""
    card = build_card(title, content, template)
    payload = json.dumps({"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)}, ensure_ascii=False)
    return succeeded(run_cmd(["lark-cli", "--profile", PROFILE, "api", "PATCH",
                              f"/open-apis/im/v1/messages/{mid}", "--data", payload,
                              "--as", "bot", "--format", "json"]))


@serialized
def send(chat_id, title, content=""):
    cache = load_cache()  # Reject corrupt/unreadable state before sending.
    card = build_card(title, content or "正在处理...", "blue")
    payload = json.dumps(card, ensure_ascii=False)
    d = run_cmd(["lark-cli", "--profile", PROFILE, "im", "+messages-send", "--chat-id", chat_id,
                 "--msg-type", "interactive", "--content", payload, "--as", "bot", "--format", "json"])
    if not (succeeded(d)):
        print(json.dumps(d.get("error", d), ensure_ascii=False), file=sys.stderr)
        return 1
    mid = d.get("data", {}).get("message_id")
    if not mid:
        print("no message_id in response", file=sys.stderr)
        return 1
    # Persist the accepted message; CardKit ID is resolved only if PATCH fails.
    cid = ""
    cache[mid] = {"card_id": cid, "seq": 0, "content": norm(content or "正在处理..."), "chat_id": chat_id}
    save_cache(cache)
    print(mid)
    return 0

def _replace(mid, title, content, template):
    cache = load_cache()  # Validate state before the network boundary.
    if im_patch(mid, title, content, template):
        st = cache.setdefault(mid, {"card_id": "", "seq": 0})
        st["content"] = norm(content)
        save_cache(cache)
        return 0
    if cardkit_update(mid, norm(content), append=False, header=template, title=title) is True:
        return 0
    print("card replacement failed", file=sys.stderr)
    return 1


@serialized
def update(mid, title, content="", append=True):
    if append:
        previous = load_cache().get(mid, {}).get("content", "")
        content = "\n".join(part for part in (previous, content) if part)
    return _replace(mid, title, content, "blue")


@serialized
def finish(mid, title, content):
    # Unknown outcome and interrupted cards must never look like successful completion.
    template = "green" if title.startswith("✅") else "yellow"
    return _replace(mid, title, content, template)


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
