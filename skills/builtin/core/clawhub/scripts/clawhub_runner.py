"""
ClawHub command runner — replaces {{ROOT_DIR}} placeholders with the actual project root path.
"""
import subprocess
from config import ROOT_DIR


def _resolve_workdir(workdir: str) -> str:
    """Replace {{ROOT_DIR}} placeholder with the actual project root path."""
    return workdir.replace("{{ROOT_DIR}}", str(ROOT_DIR))


def run_clawhub_command(command: list[str]) -> dict:
    """
    Run a clawhub command with {{ROOT_DIR}} placeholders resolved.

    Args:
        command: List of command arguments, e.g. ["install", "my-skill", "--workdir", "{{ROOT_DIR}}"]

    Returns:
        dict with keys: success (bool), stdout (str), stderr (str)
    """
    # Resolve {{ROOT_DIR}} in all arguments
    resolved = [_resolve_workdir(arg) for arg in command]

    cmd = ["npx", "--yes", "clawhub@latest"] + resolved

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "stdout": "",
            "stderr": "Command timed out after 120 seconds",
            "returncode": -1,
        }
    except FileNotFoundError:
        return {
            "success": False,
            "stdout": "",
            "stderr": "npx not found. Please install Node.js first.",
            "returncode": -1,
        }
    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Error: {e}",
            "returncode": -1,
        }
