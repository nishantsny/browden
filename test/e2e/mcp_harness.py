import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from browser_guard.web_navigator.utils.network_utils import get_free_port

class McpServerHarness:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self._proc = None
        self._log_file = None
        
        # Pick a free port
        self.port = get_free_port()
            
        self.url = f"http://127.0.0.1:{self.port}/sse"
        
    def start(self):
        env = os.environ.copy()
        env["MCP_TRANSPORT"] = "sse"
        env["MCP_HOST"] = "127.0.0.1"
        env["MCP_PORT"] = str(self.port)
        # Set explicitly in the child env
        env["XDG_CACHE_HOME"] = str(self.tmp_path)
        
        log_path = self.tmp_path / "mcp_server.log"
        self._log_file = open(log_path, "w")
        
        cmd = [sys.executable, "-m", "browser_guard.mcp.server"]
        self._proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        
        # Wait for readiness
        deadline = time.time() + 30.0
        while time.time() < deadline:
            if self._proc.poll() is not None:
                self._log_file.flush()
                with open(log_path, "r") as f:
                    log_tail = f.read()[-2000:]
                raise RuntimeError(f"MCP server died during startup. Log tail:\n{log_tail}")
                
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1.0):
                    return # Ready
            except (ConnectionRefusedError, socket.timeout, OSError):
                time.sleep(0.5)
                
        raise RuntimeError(f"MCP server failed to bind to {self.port} within 30s.")
        
    def stop(self):
        if not self._proc:
            return
            
        if self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                try:
                    self._proc.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                    self._proc.wait()
            except ProcessLookupError:
                pass
                
        if self._log_file:
            self._log_file.close()
