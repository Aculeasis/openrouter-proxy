#!/usr/bin/env python3
"""
Configuration module for OpenRouter API Proxy.
Loads settings from a YAML file and initializes logging.
"""

import logging
import sys
from typing import Dict, Any

import yaml

from constants import CONFIG_FILE


def load_config() -> Dict[str, Any]:
    """Load configuration from YAML file."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as file:
            return yaml.safe_load(file)
    except FileNotFoundError:
        print(f"Configuration file {CONFIG_FILE} not found. "
              "Please create it based on config.yml.example.")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing configuration file: {e}")
        sys.exit(1)


def setup_logging(config_: Dict[str, Any]) -> logging.Logger:
    """Configure logging based on configuration."""
    log_level_str = config_.get("server", {}).get("log_level", "INFO")
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger_ = logging.getLogger("openrouter-proxy")
    logger_.info("Logging level set to %s", log_level_str)

    return logger_


def normalize_and_validate_config(config_data: Dict[str, Any]):
    """
    Normalizes the configuration by adding defaults for missing keys
    and validates the structure and types, logging warnings/errors.
    Modifies the config_data dictionary in place.
    """
    # --- OpenRouter Section ---
    if not isinstance(config_data.get("openrouter"), dict):
        logger.warning("'openrouter' section missing or invalid in config.yml. Using defaults.")
        config_data["openrouter"] = {}
    openrouter_config = config_data["openrouter"]

    default_base_url = "https://openrouter.ai/api/v1"
    if not isinstance(openrouter_config.get("base_url"), str):
        logger.warning(
            "'openrouter.base_url' missing or invalid in config.yml. Using default: %s",
            default_base_url
        )
        openrouter_config["base_url"] = default_base_url
    # Remove trailing slash if present
    openrouter_config["base_url"] = openrouter_config["base_url"].rstrip("/")

    default_public_endpoints = ["/api/v1/models"]
    if "public_endpoints" in openrouter_config and openrouter_config["public_endpoints"] is None:
        openrouter_config["public_endpoints"] = []
    if not isinstance(openrouter_config["public_endpoints"], list):
        logger.warning(
            "'openrouter.public_endpoints' missing or invalid in config.yml. "
            "Using default: %s",
            default_public_endpoints
        )
        openrouter_config["public_endpoints"] = default_public_endpoints
    else:
        validated_endpoints = []
        for i, endpoint in enumerate(openrouter_config["public_endpoints"]):
            if not isinstance(endpoint, str):
                logger.warning("Item %d in 'openrouter.public_endpoints' is not a string. Skipping.", i)
                continue
            if not endpoint:
                logger.warning("Item %d in 'openrouter.public_endpoints' is empty. Skipping.", i)
                continue
            # Ensure leading slash
            if not endpoint.startswith("/"):
                validated_endpoints.append("/" + endpoint)
            else:
                validated_endpoints.append(endpoint)
        openrouter_config["public_endpoints"] = validated_endpoints

    if not isinstance(openrouter_config.get("keys"), list):
        logger.warning("'openrouter.keys' missing or invalid in config.yml. Using empty list.")
        openrouter_config["keys"] = []
    if not openrouter_config["keys"]:
        logger.warning(
            "'openrouter.keys' list is empty in config.yml. "
            "Proxy will not work for authenticated endpoints."
        )

    default_free_only = False
    if not isinstance(openrouter_config.get("free_only"), bool):
         logger.warning(
             "'openrouter.free_only' missing or invalid in config.yml. Using default: %s",
             default_free_only
         )
         openrouter_config["free_only"] = default_free_only

    default_google_rate_delay = 0
    if not isinstance(openrouter_config.get("google_rate_delay"), (int, float)):
         logger.warning(
             "'openrouter.google_rate_delay' missing or invalid in config.yml. "
             "Using default: %s",
             default_google_rate_delay
         )
         openrouter_config["google_rate_delay"] = default_google_rate_delay

    # --- Request Proxy Section ---
    if not isinstance(config_data.get("requestProxy"), dict):
        logger.warning("'requestProxy' section missing or invalid in config.yml. Using defaults.")
        config_data["requestProxy"] = {}
    proxy_config = config_data["requestProxy"]

    default_proxy_enabled = False
    if not isinstance(proxy_config.get("enabled"), bool):
        logger.warning(
            "'requestProxy.enabled' missing or invalid in config.yml. Using default: %s",
            default_proxy_enabled
        )
        proxy_config["enabled"] = default_proxy_enabled

    default_proxy_url = ""
    if not isinstance(proxy_config.get("url"), str):
        logger.warning(
            "'requestProxy.url' missing or invalid in config.yml. Using default: '%s'",
            default_proxy_url
        )
        proxy_config["url"] = default_proxy_url


# Load configuration
config = load_config()

# Initialize logging
logger = setup_logging(config)

# Normalize and validate configuration (modifies config in place)
normalize_and_validate_config(config)
