"""Local-only launcher. Deliberately no Lose It configuration/auth imports."""

import argparse
import os
import sys
from pathlib import Path


def command(port, data_dir):
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(Path(__file__).with_name("dashboard.py")),
        "--server.address=127.0.0.1",
        f"--server.port={port}",
        "--server.headless=true",
        "--browser.serverAddress=127.0.0.1",
        "--browser.gatherUsageStats=false",
        "--server.enableCORS=true",
        "--server.enableXsrfProtection=true",
        "--server.fileWatcherType=none",
        "--server.maxUploadSize=1",
        "--server.enableStaticServing=false",
        "--client.showErrorDetails=false",
        "--client.toolbarMode=minimal",
        "--logger.level=error",
        "--theme.base=light",
        "--theme.primaryColor=#007f88",
        "--theme.backgroundColor=#ffffff",
        "--theme.secondaryBackgroundColor=#f2f5fa",
        "--theme.textColor=#10253f",
        "--",
        "--data-dir",
        str(data_dir),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Local nutrition dashboard; no remote sync or auth."
    )
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(
            os.environ.get("LOSEIT_DATA_DIR", str(Path.home() / ".local/share/loseit-readonly"))
        ),
    )
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    data_dir = args.data_dir.expanduser().resolve()
    if not (data_dir / "nutrition.sqlite3").is_file():
        parser.error("Existing nutrition database required; run loseit-sync separately first")
    os.umask(0o077)
    os.execv(sys.executable, command(args.port, data_dir))


if __name__ == "__main__":
    main()
