"""Offline regression tests of real modules; subprocess/socket boundaries are blocked."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class Offline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="card-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "isolated home"
        self.env = patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        connect_ex = patch.object(socket.socket, "connect_ex", side_effect=AssertionError("network forbidden"))
        connect_ex.start()
        self.addCleanup(connect_ex.stop)
        self.process = patch.object(subprocess, "run", side_effect=AssertionError("unmocked subprocess"))
        self.run = self.process.start()
        self.addCleanup(self.process.stop)
        self.h = module(ROOT / "handler.py", "actual_handler")
        self.c = module(ROOT / "card-stream.py", "actual_card_stream")
        self.now = 10000.0
        clock = patch.object(self.h.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        sleep = patch.object(self.h.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        self.commands = []
        self.run.side_effect = self.card_process

    def card_process(self, cmd, **kwargs):
        self.assertEqual(cmd[:2], [sys.executable, str(ROOT / "card-stream.py")])
        self.assertEqual(kwargs["env"]["HERMES_HOME"], str(self.home))
        self.commands.append(cmd[2:])
        return subprocess.CompletedProcess(cmd, 0, "om_offline\n" if cmd[2] == "send" else "", "")

    def fire(self, event, **kwargs):
        self.h.handle(event, {"platform": "feishu", "session_id": "session", **kwargs})

    def start(self, **kwargs):
        self.fire("agent:start", chat_id="oc_offline", chat_type="dm", **kwargs)

    def step(self, iteration=2):
        self.fire("agent:step", iteration=iteration,
                  tools=[{"name": "terminal", "arguments": {"command": "ls local"}}])

    def narrate(self, text="正在检查", iteration=2, turn_id="turn"):
        self.h.observe("narration", {"platform": "feishu", "session_id": "session",
                                   "turn_id": turn_id, "iteration": iteration, "text": text})

    def bind(self, turn_id="turn"):
        self.h.observe("bind_turn", {"platform": "feishu", "session_id": "session", "turn_id": turn_id})

    def test_import_has_no_filesystem_effect(self):
        self.assertFalse(self.home.exists())

    def test_private_long_card_and_throttle(self):
        self.start()
        self.step(0)
        self.step(1)
        self.assertEqual(self.commands, [])
        self.step(2)
        self.assertEqual([c[0] for c in self.commands], ["send"])
        self.step(3)
        self.assertEqual(len(self.commands), 1)
        self.now += 2
        self.step(4)
        self.assertEqual(self.commands[-1][0], "replace")
        self.assertEqual(self.h.load_state()["session"]["message_id"], "om_offline")

    def test_short_task_no_card(self):
        self.start()
        self.step(0)
        self.step(1)
        self.fire("agent:end", response="done")
        self.assertEqual(self.commands, [])
        self.assertEqual(self.h.load_state(), {})

    def test_group_forum_unknown_fail_closed(self):
        for chat_type in ("group", "forum", "", None, "private"):
            with self.subTest(chat_type=chat_type):
                self.fire("agent:start", chat_id="oc_offline", chat_type=chat_type)
                self.step()
                self.fire("agent:end")
                self.assertEqual(self.commands, [])
        self.fire("agent:start", chat_id="oc_offline")
        self.step()
        self.assertEqual(self.commands, [])

    def test_other_platform_cron_delegation_and_missing_session_ignored(self):
        for ctx in ({"platform": "cli", "session_id": "session"},
                    {"platform": "feishu", "session_id": "cron-one"},
                    {"platform": "feishu", "session_id": "deleg-one"},
                    {"platform": "feishu"}):
            self.h.handle("agent:start", {**ctx, "chat_id": "oc_offline", "chat_type": "dm"})
        self.assertFalse(self.home.exists())

    def test_narration_replaces_same_round_multiple_tools(self):
        self.start()
        self.bind()
        self.fire("agent:step", iteration=1, tools=[{"name": "terminal"}, {"name": "read_file"}])
        self.narrate(iteration=1)
        self.assertEqual(self.h.load_state()["session"]["steps"], [self.h.NARRATION + "正在检查"])
        self.step(2)
        self.assertIn("正在检查", self.commands[-1][-1])
        self.assertNotIn(self.h.NARRATION, self.commands[-1][-1])

    def test_narration_before_step_suppresses_fallback_only_same_iteration(self):
        self.start()
        self.bind()
        self.narrate(iteration=2)
        self.step(2)
        self.assertNotIn("查看目录", self.commands[-1][-1])
        self.now += 2
        self.step(3)
        self.assertIn("查看目录", self.commands[-1][-1])

    def test_unbound_and_stale_narration_ignored(self):
        self.start()
        self.narrate(text="unbound")
        self.bind()
        self.bind("old")
        self.narrate(text="stale", turn_id="old")
        self.step()
        self.assertNotIn("stale", self.commands[-1][-1])
        self.assertNotIn("unbound", self.commands[-1][-1])

    def test_end_success_or_failure_text_cannot_claim_delivery(self):
        for response in ("success", "error: provider failed"):
            with self.subTest(response=response):
                self.start()
                self.step()
                self.fire("agent:end", response=response, model="deepseek-unrecognized")
                end = self.commands[-1]
                self.assertEqual(end[0], "finish")
                self.assertIn("投递状态未知", end[2])
                self.assertIn("token 未知", end[3])
                self.assertIn("成本未知", end[3])
                self.assertNotIn(response, end[3])
                self.assertNotIn("✅", end[2])
                self.assertEqual(self.h.load_state(), {})

    def test_unregistered_delivery_success_failure_not_invented(self):
        self.start()
        self.step()
        for success in (True, False):
            self.fire("agent:delivery", success=success)
            self.fire("delivery:complete", success=success)
        self.assertEqual([c[0] for c in self.commands], ["send"])
        self.assertEqual(self.h.load_state()["session"]["phase"], "running")
        self.fire("agent:end")
        self.assertIn("投递状态未知", self.commands[-1][2])

    def test_stale_cleanup_uses_activity_not_throttled_update(self):
        self.start()
        self.step()
        self.now += self.h.STALE_SECONDS - 1
        self.narrate()  # Unbound ignored: no activity refresh.
        self.step(3)
        self.now += self.h.STALE_SECONDS - 1
        self.fire("agent:start", session_id="other", chat_id="oc_offline", chat_type="dm")
        self.assertIn("session", self.h.load_state())
        self.now += 2
        self.fire("agent:step", session_id="other", iteration=0)
        self.assertNotIn("session", self.h.load_state())
        self.assertIn("任务中断", self.commands[-1][2])

    def test_new_start_interrupts_old_card_and_rejects_queued_old_narration(self):
        self.start()
        self.bind("old")
        self.step()
        self.start()
        self.bind("new")
        self.narrate(text="old text", turn_id="old")
        self.assertIn("任务中断", self.commands[-1][2])
        self.assertEqual(self.h.load_state()["session"]["steps"], [])

    def test_send_timeout_never_retried_and_attempt_persisted_before_boundary(self):
        def timeout(cmd, **kwargs):
            self.assertTrue(self.h.load_state()["session"]["send_attempted"])
            raise subprocess.TimeoutExpired(cmd, 30)
        self.run.side_effect = timeout
        self.start()
        self.step()
        self.step(3)
        self.assertEqual(self.run.call_count, 1)
        self.assertIsNone(self.h.load_state()["session"]["message_id"])

    def test_finish_failure_retained_then_retried(self):
        self.start()
        self.step()
        self.run.side_effect = lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, "", "sensitive error")
        self.fire("agent:end")
        self.assertEqual(self.h.load_state()["session"]["phase"], "terminal")
        self.assertEqual(self.run.call_count, 4)  # send + three idempotent finish attempts
        self.run.side_effect = self.card_process
        self.fire("agent:start", session_id="other", chat_id="oc_offline", chat_type="dm")
        self.assertNotIn("session", self.h.load_state())

    def test_update_retry_succeeds(self):
        self.start()
        self.step()
        self.now += 2
        self.run.side_effect = [subprocess.TimeoutExpired("offline", 30),
                                subprocess.CompletedProcess([], 0, "", "")]
        self.step(3)
        self.assertEqual(self.run.call_count, 3)
        self.assertEqual(self.h.load_state()["session"]["last_update"], self.now)

    def test_corrupt_state_fails_closed(self):
        self.home.joinpath("cache").mkdir(parents=True)
        self.h.state_path().write_text("{broken", encoding="utf-8")
        self.start()
        self.step()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.h.state_path().read_text(), "{broken")

    def test_malformed_context_error_isolated(self):
        self.h.handle("agent:start", ["bad"])
        self.start()
        self.fire("agent:step", iteration="bad")
        self.assertEqual(self.commands, [])
        self.assertEqual(self.h.load_state()["session"]["phase"], "running")

    def test_exact_model_prices_only(self):
        for model in ("deepseek-unrecognized", "deepseek-v4-flash-0731", "other/deepseek-v4-flash", ""):
            self.assertIsNone(self.h.estimate_turn_cost(model, 100, 10, 0, 0))
        self.assertAlmostEqual(self.h.estimate_turn_cost("deepseek-v4-flash", 100, 10, 0, 0), 0.00012)

    def test_two_threads_do_not_duplicate_send_or_lose_sessions(self):
        self.start()
        threads = [threading.Thread(target=self.step) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertEqual([c[0] for c in self.commands], ["send"])
        self.assertEqual(self.h.load_state()["session"]["tool_count"], 2)

    def cli_boundary(self, replies=None):
        replies = iter(replies or [{"code": 0}])
        def fake(cmd, **kwargs):
            self.assertEqual(cmd[:3], ["lark-cli", "--profile", "cli_a93011294a39dbc6"])
            self.commands.append(cmd)
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return subprocess.CompletedProcess(cmd, 0, json.dumps(reply), "")
        self.run.side_effect = fake

    def test_helper_send_has_fixed_profile_and_portable_cache(self):
        self.cli_boundary([{"code": 0, "data": {"message_id": "om_offline"}}])
        with patch.dict(os.environ, {"LARK_CLI_PROFILE": "ignored"}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.c.send("oc_offline", "working", "content"), 0)
        self.assertEqual(output.getvalue().strip(), "om_offline")
        self.assertEqual(self.c.cache_path().parent, self.home / "cache")
        self.assertEqual(len(self.commands), 1)

    def test_helper_warning_yellow_success_green_and_replacement(self):
        for title, color in (("⚠️ 投递状态未知", "yellow"), ("✅ 手动完成", "green")):
            self.cli_boundary()
            self.assertEqual(self.c.finish("om_offline", title, "full body"), 0)
            cmd = self.commands[-1]
            card = json.loads(json.loads(cmd[cmd.index("--data") + 1])["content"])
            self.assertEqual(card["header"]["template"], color)
            self.assertEqual(card["body"]["elements"][0]["content"], "full body")

    def test_helper_cardkit_fallback_title_color_no_duplicate_body(self):
        self.c.save_cache({"om_offline": {"card_id": "card_offline", "content": "old", "seq": 4}})
        self.cli_boundary([{"code": 1}, {"code": 0}])
        self.assertEqual(self.c.finish("om_offline", "⚠️ ended", "new"), 0)
        cmd = self.commands[-1]
        body = json.loads(cmd[cmd.index("--data") + 1])
        actions = json.loads(body["actions"])
        self.assertEqual(body["sequence"], 5)
        self.assertEqual(actions[0]["params"]["partial_element"]["content"], "new")
        self.assertEqual(actions[1]["params"]["settings"]["header"]["title"]["content"], "⚠️ ended")

    def test_helper_failed_transport_no_extra_message_fallback(self):
        self.cli_boundary([subprocess.TimeoutExpired("offline", 8), {"code": 1}])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.c.finish("om_offline", "⚠️ ended", "body"), 1)
        self.assertEqual(len(self.commands), 2)
        self.assertTrue(all(c[0] == "lark-cli" for c in self.commands))

    def test_helper_corrupt_state_stops_send_and_finish_before_network(self):
        self.home.joinpath("cache").mkdir(parents=True)
        self.c.cache_path().write_text("{bad", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.c.send("oc_offline", "work"), 1)
            self.assertEqual(self.c.finish("om_offline", "⚠️ ended", "body"), 1)
        self.assertEqual(self.run.call_count, 0)

    def test_write_permission_error_isolated_and_prevents_send(self):
        self.start()
        with patch.object(self.h._storage, "save", side_effect=PermissionError("offline denied")):
            self.step()
        self.assertEqual(self.run.call_count, 0)
        self.assertIsNone(self.h.load_state()["session"]["message_id"])

    def test_helper_nonzero_return_and_bad_json_are_failures(self):
        for result in (subprocess.CompletedProcess([], 1, '{"code":0}', ""),
                       subprocess.CompletedProcess([], 0, "not json", "")):
            self.run.side_effect = None
            self.run.return_value = result
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.c.send("oc_offline", "work"), 1)
        with self.assertRaises(ValueError):
            self.c.run_cmd(["lark-cli", "im", "+messages-send"])


@unittest.skipUnless(os.environ.get("HERMES_NATIVE_SOURCE"), "optional read-only native integration")
class NativeContract(unittest.TestCase):
    setUp = Offline.setUp
    card_process = Offline.card_process
    def test_native_registry_plugin_registration_and_queued_dispatch(self):
        sys.path.insert(0, os.environ["HERMES_NATIVE_SOURCE"])
        self.addCleanup(lambda: sys.path.remove(os.environ["HERMES_NATIVE_SOURCE"]))
        import providers
        discovery = patch.object(providers, "_run_discovery_steps")
        discovery.start()
        self.addCleanup(discovery.stop)
        from gateway.hooks import HookRegistry
        from hermes_cli import plugins
        from agent import plugin_stream_hooks
        package = self.home / "hooks" / "feishu-card-progress"
        package.mkdir(parents=True)
        for name in ("HOOK.yaml", "handler.py", "card-stream.py", "storage.py"):
            shutil.copyfile(ROOT / name, package / name)
        # No discovery of installed plugins or loading of any real config.
        manager = plugins.PluginManager()
        manifest = plugins.parse_manifest_file(ROOT / "observer" / "plugin.yaml", ROOT / "observer", "project", "")
        observer = module(ROOT / "observer" / "__init__.py", "actual_observer")
        observer.register(plugins.PluginContext(manifest, manager))
        self.assertEqual(set(manager._hooks), {"pre_llm_call", "on_interim_message"})
        registry = HookRegistry()
        registry.discover_and_load()
        self.assertEqual(len(registry.loaded_hooks), 1)
        import asyncio
        asyncio.run(registry.emit("agent:start", {"platform": "feishu", "session_id": "session",
                                                  "chat_id": "oc_offline", "chat_type": "dm"}))
        with patch.object(plugins, "_resolve_hook_callback_timeout", return_value=0):
            self.assertEqual(manager.invoke_hook("pre_llm_call", platform="feishu", session_id="session", turn_id="turn"), [])
        with patch.object(plugins, "iter_hook_callbacks", side_effect=lambda name: tuple(manager._hooks.get(name, []))):
            self.assertTrue(plugin_stream_hooks.enqueue_plugin_stream_hook(
                "on_interim_message", surface="feishu", session_id="session", turn_id="turn",
                iteration=2, text="原生旁白", already_streamed=False))
            for dispatcher in plugin_stream_hooks._dispatchers_for("on_interim_message"):
                dispatcher.events.join()
            plugin_stream_hooks.shutdown_plugin_stream_hook_dispatcher(timeout=1)
        # The actual subprocess boundary is still mocked; deployed sibling path differs.
        self.run.side_effect = lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "om_native\n", "")
        asyncio.run(registry.emit("agent:step", {"platform": "feishu", "session_id": "session",
                                                 "iteration": 2, "tools": [{"name": "terminal"}]}))
        self.assertIn("原生旁白", self.run.call_args.args[0][-1])
        self.assertNotIn("终端", self.run.call_args.args[0][-1])
        asyncio.run(registry.emit("agent:end", {"platform": "feishu", "session_id": "session", "response": "done"}))
        self.assertIn("投递状态未知", self.run.call_args.args[0][-2])
        self.assertEqual(self.h.load_state(), {})


if __name__ == "__main__":
    unittest.main()
