"""Starts Promin: `python main.py`. The code is in the `promin` package."""
import logging

import uvicorn

from promin.app import create_app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_app()
    # One process: the queue and the remote state live in memory.
    uvicorn.run(app, host="0.0.0.0", port=app.state.services.settings.port)
