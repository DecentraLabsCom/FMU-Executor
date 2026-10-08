"""Entry point: ``python -m app`` starts the FMU Executor service."""

import uvicorn
from . import config

def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=config.bind_host(),
        port=config.bind_port(),
        log_level=config.log_level().lower(),
    )


if __name__ == "__main__":
    main()
