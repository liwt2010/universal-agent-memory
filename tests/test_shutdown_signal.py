"""Tests for graceful shutdown and OS signal handling.

Bug 7 regression test: docker_entrypoint.py must call
UniversalMemorySystem.register_signal_handlers() so that SIGTERM
(via `docker stop`) triggers shutdown() instead of Python exiting
hard and dropping working-tier memories.
"""

import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# Ensure `src/` is on sys.path so `import uams.*` works without an editable install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestRegisterSignalHandlers(unittest.TestCase):
    """UniversalMemorySystem.register_signal_handlers must install
    real signal.signal() handlers for SIGTERM and SIGINT."""

    def _make_ums(self):
        from uams import UniversalMemorySystem
        return UniversalMemorySystem()

    def test_sigterm_handler_installed(self):
        from uams import UniversalMemorySystem
        ums = self._make_ums()
        try:
            ums.register_signal_handlers()
            handler = signal.getsignal(signal.SIGTERM)
            self.assertIsNotNone(handler)
            self.assertNotEqual(handler, signal.SIG_DFL)
            self.assertNotEqual(handler, signal.SIG_IGN)
        finally:
            # Restore default so we don't leave a global SIGTERM hook
            # registered for the rest of the test process.
            signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def test_sigint_handler_installed(self):
        from uams import UniversalMemorySystem
        ums = self._make_ums()
        try:
            ums.register_signal_handlers()
            handler = signal.getsignal(signal.SIGINT)
            self.assertIsNotNone(handler)
            self.assertNotEqual(handler, signal.SIG_DFL)
            self.assertNotEqual(handler, signal.SIG_IGN)
        finally:
            signal.signal(signal.SIGINT, signal.SIG_DFL)


class TestDockerEntrypointRegistersSignals(unittest.TestCase):
    """The container's docker_entrypoint.py is the production entry
    point. It MUST call ums.register_signal_handlers() — otherwise a
    `docker stop` (SIGTERM) will hard-exit Python and drop working-tier
    memories before UniversalMemorySystem.shutdown() can run.

    We verify by importing the module and checking the main() function
    source for the call. This is a structural test — if someone later
    refactors main(), they must keep the register_signal_handlers call."""

    def test_docker_entrypoint_calls_register_signal_handlers(self):
        import ast
        entrypoint = Path(__file__).resolve().parent.parent / "docker_entrypoint.py"
        source = entrypoint.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find the main() function
        main_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                main_fn = node
                break
        self.assertIsNotNone(main_fn, "main() not found in docker_entrypoint.py")

        # Walk the body of main() looking for a register_signal_handlers() call
        calls = [n for n in ast.walk(main_fn) if isinstance(n, ast.Call)]
        called_methods = {
            ast.unparse(c.func) if hasattr(ast, "unparse") else _name_of(c.func)
            for c in calls
        }
        self.assertTrue(
            any("register_signal_handlers" in m for m in called_methods),
            f"docker_entrypoint.main() does not call "
            f"register_signal_handlers(). Calls found: {sorted(called_methods)}",
        )


def _name_of(node):
    """Fallback for Python <3.9 (we target 3.9+ so ast.unparse exists)."""
    if isinstance(node, ast.Attribute):
        return f"{_name_of(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


if __name__ == "__main__":
    unittest.main()