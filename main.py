#!/usr/bin/env python3
"""
OpenRouter API Proxy
Proxies requests to OpenRouter API and rotates API keys to bypass rate limits.
"""

import uvicorn
from fastapi import FastAPI

from config import config, logger
from routes import router, lifespan
from utils import get_local_ip

# Create FastAPI app
app = FastAPI(
    title="OpenRouter API Proxy",
    description="Proxies requests to OpenRouter API and rotates API keys to bypass rate limits",
    version="1.0.0",
    lifespan=lifespan,
)

# Include routes
app.include_router(router)

# Entry point
if __name__ == "__main__":
    host = config["server"]["host"]
    port = config["server"]["port"]

    # If host is 0.0.0.0, use actual local IP for display
    display_host = get_local_ip() if host == "0.0.0.0" else host

    logger.warning("Starting OpenRouter Proxy on %s:%s", host, port)
    logger.warning("API URL: http://%s:%s/api/v1", display_host, port)
    logger.info("Health check: http://%s:%s/health", display_host, port)

    # Configure log level for HTTP access logs
    log_config = uvicorn.config.LOGGING_CONFIG
    http_log_level = config["server"].get("http_log_level", "INFO").upper()
    log_config["loggers"]["uvicorn.access"]["level"] = http_log_level
    logger.info("HTTP access log level set to %s", http_log_level)

    uvicorn.run("main:app", host=host, port=port, log_config=log_config, reload=True)
