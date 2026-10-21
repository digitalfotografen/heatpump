"""Rendering the systemd unit and asking systemd about it."""

import subprocess
from importlib.resources import files
from pathlib import Path
from string import Template

SERVICE_NAME = "heatpump"
DEFAULT_UNIT_PATH = Path("/etc/systemd/system/heatpump.service")
DEFAULT_CONFIG_PATH = Path("/etc/heatpump/config.yaml")
DEFAULT_ENV_FILE = Path("/etc/heatpump/env")


def render_unit(user: str, executable: Path, config: Path, env_file: Path) -> str:
    """Fill the packaged unit template; ``executable`` is the venv's ``heatpump`` script."""
    template = Template(files("heatpump").joinpath("templates/heatpump.service").read_text())
    return template.substitute(user=user, executable=executable, config=config, env_file=env_file)


def systemctl_status() -> int:
    """Run ``systemctl status`` and return its exit code (3 means the unit is not active).

    :raises FileNotFoundError: if ``systemctl`` is not installed (not a systemd machine).
    """
    command = ["systemctl", "status", SERVICE_NAME, "--no-pager"]
    return subprocess.run(command, check=False).returncode
