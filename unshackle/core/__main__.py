import atexit
import logging

import click
import urllib3
from rich import traceback
from rich.console import Group
from rich.padding import Padding
from rich.text import Text
from urllib3.exceptions import InsecureRequestWarning

from unshackle.core import __version__
from unshackle.core.commands import Commands
from unshackle.core.config import config
from unshackle.core.console import ComfyRichHandler, console
from unshackle.core.constants import context_settings
from unshackle.core.update_checker import UpdateChecker
from unshackle.core.utilities import close_debug_logger, init_debug_logger


@click.command(cls=Commands, invoke_without_command=True, context_settings=context_settings)
@click.option("-v", "--version", is_flag=True, default=False, help="Print version information.")
@click.option("-d", "--debug", is_flag=True, default=False, help="Enable DEBUG level logs and JSON debug logging.")
def main(version: bool, debug: bool) -> None:
    """unshackle—Modular Movie, TV, and Music Archival Software."""
    debug_logging_enabled = debug or config.debug

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(message)s",
        handlers=[
            ComfyRichHandler(
                show_time=False,
                show_path=debug,
                console=console,
                rich_tracebacks=True,
                tracebacks_suppress=[click],
                log_renderer=console._log_render,  # noqa
            )
        ],
    )

    if debug_logging_enabled:
        init_debug_logger(enabled=True)

    urllib3.disable_warnings(InsecureRequestWarning)

    traceback.install(console=console, width=80, suppress=[click])

    console.print(
    Padding(
        Group(
            Text(
            r"░█▀▀░█▀█░█░█░▀█▀░█▀▀░█▀▄" + "\n"
            r"░█▀▀░█░█░▀▄▀░░█░░█▀▀░█░█" + "\n"
            r"░▀▀▀░▀░▀░░▀░░▀▀▀░▀▀▀░▀▀░" + "\n" ,
                style="ascii.art",
            ),
            Text("    and more than unshackled...", style = "ascii.art"),
            f"\nv [repr.number]{__version__}[/] - https://github.com/vinefeeder/envied",
        ),
        (1, 11, 1, 10),
        expand=True,
    ),
    justify="center",
    )




@atexit.register
def cleanup():
    """Clean up resources on exit."""
    close_debug_logger()


if __name__ == "__main__":
    main()
