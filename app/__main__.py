"""Entry point: ``python -m app`` starts the FMU Executor service."""

import uvicorn
from . import config

uvicorn.run(
    "app.main:app",
    host=config.bind_host(),
    port=config.bind_port(),
    log_level=config.log_level().lower(),
)
