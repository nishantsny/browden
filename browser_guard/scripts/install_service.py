import argparse
import os
import sys
from pathlib import Path

def generate_service():
    parser = argparse.ArgumentParser(description="Install Browser Guard as a systemd service.")
    parser.add_argument("--port", type=int, default=8000, help="Port for the SSE server (default: 8000)")
    parser.add_argument("--python", type=str, default=sys.executable, help="Path to Python executable (default: current sys.executable)")
    parser.add_argument("--root", type=str, default=str(Path(__file__).resolve().parent.parent.parent), help="Project root directory")
    parser.add_argument("--display", type=str, default=os.environ.get("DISPLAY", ":0"), help="DISPLAY environment variable (default: :0 or current)")
    
    args = parser.parse_args()

    service_content = f"""[Unit]
Description=Browser Guard MCP Server (SSE)
After=network.target

[Service]
Environment=DISPLAY={args.display}
Environment=MCP_TRANSPORT=sse
Environment=MCP_PORT={args.port}
WorkingDirectory={args.root}
ExecStart={args.python} -m browser_guard.mcp.server
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
    
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True, exist_ok=True)
    
    service_path = systemd_dir / "browser-guard.service"
    service_path.write_text(service_content)
    print(f"Service file generated at: {service_path}")
    print("To enable and start the service, run:")
    print("  systemctl --user daemon-reload")
    print(f"  systemctl --user enable --now browser-guard")

if __name__ == "__main__":
    generate_service()
