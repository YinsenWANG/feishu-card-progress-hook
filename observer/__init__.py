"""Read-only native plugin observers. No prompts, stream transforms, or core patches."""
import importlib.util
from pathlib import Path


def register(ctx):
    # Hook package is a sibling deployment under the SAME active HERMES_HOME.
    # Resolve at callback time: multiplex gateway workers retain profile contextvars.
    def dispatch(event, payload):
        try:
            from hermes_constants import get_hermes_home
        except ImportError:
            import os
            home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        else:
            home = get_hermes_home()
        path = home / "hooks" / "feishu-card-progress" / "handler.py"
        if not path.is_file():
            return
        spec = importlib.util.spec_from_file_location("card_progress_observer_handler", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.observe(event, payload)

    def before_turn(session_id="", turn_id="", platform="", **kwargs):
        if platform == "feishu":
            dispatch("bind_turn", {"platform": platform, "session_id": session_id, "turn_id": turn_id})
        return None

    def narration(session_id="", turn_id="", surface="", text="", iteration=0, **kwargs):
        if surface == "feishu":
            dispatch("narration", {"platform": surface, "session_id": session_id,
                                   "turn_id": turn_id, "text": text, "iteration": iteration})
        return None

    ctx.register_hook("pre_llm_call", before_turn)
    ctx.register_hook("on_interim_message", narration)
