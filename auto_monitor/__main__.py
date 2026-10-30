"""Entry point: ``python -m auto_monitor [config.yaml]`` or ``auto-monitor-k8s``."""

import sys

from .config import ConfigError, load_config
from .log import setup_logging
from .service import MonitorService


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        config = load_config(path)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.log_level, config.log_format)
    try:
        MonitorService(config).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
