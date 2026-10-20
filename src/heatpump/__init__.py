"""Heat pump price controller."""

from importlib.metadata import version

__version__ = version("heatpump")


def main() -> None:
    """Script entry point: ``heatpump`` on the command line."""
    from heatpump.cli import app

    app()
