#!/usr/bin/env python3
"""
API routes for OpenRouter API Proxy.
"""

import json
from typing import Optional, Dict, Any, AsyncGenerator

import httpx
from fastapi import APIRouter, Request, Header, HTTPException
from fastapi.responses import StreamingResponse, Response
from openai import AsyncOpenAI

from config import config, logger
from constants import OPENROUTER_BASE_URL, PUBLIC_ENDPOINTS, BINARY_ENDPOINTS
from key_manager import KeyManager
from utils import (
    verify_access_key,
    check_rate_limit_error,
)

# Create router
router = APIRouter()

# Initialize key manager
key_manager = KeyManager(
    keys=config["openrouter"]["keys"],
    cooldown_seconds=config["openrouter"]["rate_limit_cooldown"],
)


# Function to create OpenAI client with the right API key
async def get_openai_client(api_key: str) -> AsyncOpenAI:
    """Create an OpenAI client with the specified API key."""
    return AsyncOpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL)


@router.api_route(
    "/api/v1{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"],
)
async def proxy_endpoint(
    request: Request, path: str, authorization: Optional[str] = Header(None)
):
    """
    Main proxy endpoint for handling all requests to OpenRouter API.
    """
    is_public = any(f"/api/v1{path}".startswith(ep) for ep in PUBLIC_ENDPOINTS)
    is_binary = any(f"/api/v1{path}".startswith(ep) for ep in BINARY_ENDPOINTS)

    # Verify authorization for non-public endpoints
    if not is_public:
        await verify_access_key(
            authorization=authorization,
            access_key=config["server"]["access_key"],
        )

    # Log the full request URL including query parameters
    full_url = str(request.url).replace(str(request.base_url), "/")
    logger.info(
        "Proxying request to %(full_url)s (Public: %(is_public)s, Binary: %(is_binary)s)",
        {"full_url": full_url, "is_public": is_public, "is_binary": is_binary},
    )

    # Parse request body (if any)
    request_body = None
    is_stream = False

    try:
        body_bytes = await request.body()
        if body_bytes:
            request_body = json.loads(body_bytes)
            is_stream = request_body.get("stream", False)

            # Log if this is a streaming request
            if is_stream and "/chat/completions" in path:
                logger.info("Detected streaming request")

            # Check for model variant
            if "/chat/completions" in path and request.method == "POST":
                model = request_body.get("model", "")
                if (
                    ":" in model
                ):  # This indicates a model variant like :free, :beta, etc.
                    base_model, variant = model.split(":", 1)
                    model_variant = f"{base_model} with {variant} tier"
                    logger.info(f"Using model variant: {model_variant}")

    except Exception as e:
        logger.debug(f"Could not parse request body: {str(e)}")
        request_body = None

    # For binary, models endpoint, non-OpenAI-compatible endpoints or requests with model-specific parameters, fall back to httpx
    if is_binary or "/models" in path or not "/chat/completions" in path:
        return await proxy_with_httpx(request, path, is_public, is_binary, is_stream)

    # For OpenAI-compatible endpoints, use the OpenAI library
    try:
        # Get API key to use
        if not is_public:
            api_key = await key_manager.get_next_key()
            if not api_key:
                raise HTTPException(status_code=503, detail="No available API keys")
        else:
            # For public endpoints, we don't need an API key
            api_key = ""

        # Create an OpenAI client
        client = await get_openai_client(api_key)

        # Process based on the endpoint
        if "/chat/completions" in path:
            return await handle_chat_completions(
                client, request, request_body, api_key, is_stream
            )
        else:
            # Fallback for other endpoints
            return await proxy_with_httpx(
                request, path, is_public, is_binary, is_stream
            )

    except Exception as e:
        logger.error(f"Error proxying request: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Proxy error: {str(e)}")


async def handle_chat_completions(
    client: AsyncOpenAI,
    request: Request,
    request_body: Dict[str, Any],
    api_key: str,
    is_stream: bool = False,
) -> Response:
    """Handle chat completions using the OpenAI client."""
    try:
        # Extract headers to forward
        forward_headers = {}
        for k, v in request.headers.items():
            if k.lower() in ["http-referer", "x-title"]:
                forward_headers[k] = v

        # Create a copy of the request body to modify
        completion_args = request_body.copy()

        # Move non-standard parameters that OpenAI SDK doesn't support directly to extra_body
        extra_body = {}
        openai_unsupported_params = ["include_reasoning", "transforms", "route"]
        for param in openai_unsupported_params:
            if param in completion_args:
                extra_body[param] = completion_args.pop(param)

        # Ensure we don't pass 'stream' twice
        if "stream" in completion_args:
            del completion_args["stream"]

        # Create a properly formatted request to the OpenAI API
        if is_stream:
            logger.info("Making streaming chat completion request")

            response = await client.chat.completions.create(
                **completion_args, extra_headers=forward_headers, extra_body=extra_body, stream=True
            )

            # Handle streaming response
            async def stream_response() -> AsyncGenerator[bytes, None]:
                try:
                    async for chunk in response:
                        # Convert chunk to the expected SSE format
                        if chunk.choices:
                            yield f"data: {json.dumps(chunk.model_dump())}\n\n".encode(
                                "utf-8"
                            )

                    # Send the end marker
                    yield b"data: [DONE]\n\n"
                except Exception as e:
                    logger.error(f"Error in streaming response: {str(e)}")
                    # Check if this is a rate limit error
                    if "rate limit" in str(e).lower() and api_key:
                        logger.warning(f"Rate limit detected in stream. Disabling key.")
                        await key_manager.disable_key(
                            api_key, None
                        )  # Disable without reset time

            # Return a streaming response
            return StreamingResponse(
                stream_response(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            # Non-streaming request
            logger.info("Making regular chat completion request")

            response = await client.chat.completions.create(
                **completion_args, extra_headers=forward_headers, extra_body=extra_body
            )

            # Return the response as JSON
            return Response(
                content=json.dumps(response.model_dump()), media_type="application/json"
            )
    except Exception as e:
        logger.error(f"Error in chat completions: {str(e)}")
        # Check if this is a rate limit error
        if "rate limit" in str(e).lower() and api_key:
            logger.warning(
                f"Rate limit reached for API key. Disabling key and retrying."
            )
            await key_manager.disable_key(api_key, None)

            # Try again with a new key
            new_api_key = await key_manager.get_next_key()
            if new_api_key:
                new_client = await get_openai_client(new_api_key)
                return await handle_chat_completions(
                    new_client, request, request_body, new_api_key, is_stream
                )

        # Raise the exception
        raise HTTPException(
            status_code=500, detail=f"Error processing chat completion: {str(e)}"
        )


async def proxy_with_httpx(
    request: Request,
    path: str,
    is_public: bool,
    is_binary: bool,
    is_stream: bool,
) -> Response:
    """Fall back to httpx for endpoints not supported by the OpenAI SDK."""
    async with httpx.AsyncClient(timeout=60.0) as client:  # Increase default timeout
        try:
            # Special handling for models endpoint
            if "/models" in path:
                logger.info("Handling models endpoint with direct httpx")

            if is_public:
                # For public endpoints, just forward without authentication
                req_kwargs = {
                    "method": request.method,
                    "url": f"{OPENROUTER_BASE_URL}{path}",
                    "headers": {
                        k: v
                        for k, v in request.headers.items()
                        if k.lower() not in ["host", "content-length", "connection"]
                    },
                    "content": await request.body(),
                }
                # Add query parameters if they exist
                if request.query_params:
                    req_kwargs["url"] = f"{req_kwargs['url']}?{request.url.query}"
            else:
                # For authenticated endpoints, use API key rotation
                api_key = await key_manager.get_next_key()
                headers = {
                    k: v
                    for k, v in request.headers.items()
                    if k.lower()
                    not in ["host", "content-length", "connection", "authorization"]
                }
                headers["Authorization"] = f"Bearer {api_key}"

                req_kwargs = {
                    "method": request.method,
                    "url": f"{OPENROUTER_BASE_URL}{path}",
                    "headers": headers,
                    "content": await request.body(),
                }
                # Add query parameters if they exist
                if request.query_params:
                    req_kwargs["url"] = f"{req_kwargs['url']}?{request.url.query}"

            # Get the API key we're using for this request
            current_key = (
                req_kwargs["headers"]["Authorization"].replace("Bearer ", "")
                if "Authorization" in req_kwargs["headers"]
                else None
            )

            # Make the request to OpenRouter
            try:
                openrouter_resp = await client.request(**req_kwargs)

                # Special handling for models endpoint
                if "/models" in path and openrouter_resp.status_code >= 400:
                    logger.error(
                        f"Error fetching models: {openrouter_resp.status_code}"
                    )
                    error_body = await openrouter_resp.aread()
                    logger.error(
                        f"Models endpoint error response: {error_body.decode('utf-8', errors='replace')}"
                    )

            except httpx.ConnectError as e:
                logger.error(
                    f"Connection error to OpenRouter at {req_kwargs['url']}: {str(e)}"
                )
                if "/models" in path:
                    # For models endpoint, we'll return a basic error response
                    return Response(
                        content=json.dumps(
                            {
                                "error": "Could not connect to OpenRouter API",
                                "details": str(e),
                            }
                        ),
                        status_code=503,
                        media_type="application/json",
                    )
                raise HTTPException(
                    status_code=503, detail="Unable to connect to OpenRouter API"
                )

            # Handle binary responses
            if is_binary:

                async def stream_binary():
                    async for chunk in openrouter_resp.aiter_bytes():
                        yield chunk

                return StreamingResponse(
                    stream_binary(),
                    status_code=openrouter_resp.status_code,
                    headers=dict(openrouter_resp.headers),
                )

            # Handle streaming responses
            if is_stream:
                content_type = openrouter_resp.headers.get("content-type", "").lower()
                if "text/event-stream" in content_type:

                    async def stream_sse():
                        async for line in openrouter_resp.aiter_lines():
                            if line.startswith("data: "):
                                data = line[6:]  # Get data without 'data: ' prefix
                                if data == "[DONE]":
                                    yield f"data: [DONE]\n\n".encode("utf-8")
                                else:
                                    # Forward the original data without reformatting
                                    yield f"{line}\n\n".encode("utf-8")
                            elif line:
                                yield f"{line}\n\n".encode("utf-8")

                    return StreamingResponse(
                        stream_sse(),
                        media_type="text/event-stream",
                        headers={
                            "Cache-Control": "no-cache",
                            "Connection": "keep-alive",
                            "X-Accel-Buffering": "no",
                        },
                    )

            # Regular non-streaming response
            # Check for rate limit errors
            has_rate_limit_error, reset_time_ms = check_rate_limit_error(
                openrouter_resp
            )

            if has_rate_limit_error and current_key:
                logger.warning(
                    f"Rate limit reached for API key. Disabling key and retrying."
                )
                await key_manager.disable_key(current_key, reset_time_ms)

                # Retry with a new key
                new_api_key = await key_manager.get_next_key()
                if not new_api_key:
                    raise HTTPException(
                        status_code=429, detail="Rate limited and no available API keys"
                    )

                # Update the authorization header
                req_kwargs["headers"]["Authorization"] = f"Bearer {new_api_key}"
                openrouter_resp = await client.request(**req_kwargs)

            # Return the response
            return Response(
                content=await openrouter_resp.aread(),
                status_code=openrouter_resp.status_code,
                headers=dict(openrouter_resp.headers),
            )

        except httpx.ConnectError as e:
            logger.error(f"Connection error to OpenRouter: {str(e)}")
            raise HTTPException(
                status_code=503, detail="Unable to connect to OpenRouter API"
            )
        except httpx.TimeoutException as e:
            logger.error(f"Timeout connecting to OpenRouter: {str(e)}")
            raise HTTPException(
                status_code=504, detail="OpenRouter API request timed out"
            )
        except Exception as e:
            logger.error(f"Error proxying request with httpx: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Proxy error: {str(e)}")


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}
