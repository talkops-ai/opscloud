"""Starlette custom routes application (`http.app`) mounted into the LangGraph Agent Server.

Exposes:
- `GET /webhooks` — Discovery endpoint listing all supported webhook providers and active routes.
- `GET /webhooks/deliveries` — Recent accepted webhook deliveries and mapped LangGraph thread IDs.
- `POST /webhooks/{provider}` — Unified inbound webhook endpoint for SNS, PagerDuty, Alertmanager,
  CloudEvents, Jenkins, GitHub, Datadog, ServiceNow, Azure Monitor, and Generic JSON payloads.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from opscloud.webhooks.dispatcher import WebhookDispatcher

_dispatcher: WebhookDispatcher | None = None


def get_webhook_dispatcher() -> WebhookDispatcher:
    """Return or initialize the singleton WebhookDispatcher for the server process."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = WebhookDispatcher()
    return _dispatcher


def set_webhook_dispatcher(dispatcher: WebhookDispatcher | None) -> None:
    """Override the active WebhookDispatcher (used for testing and custom injection)."""
    global _dispatcher
    _dispatcher = dispatcher


async def list_webhooks_endpoint(request: Request) -> JSONResponse:
    """Return supported webhook providers, aliases, and configuration summary."""
    dispatcher = get_webhook_dispatcher()
    providers = dispatcher.registry.list_providers()
    return JSONResponse(
        {
            "status": "ok",
            "service": "opscloud-webhook-server",
            "providers": providers,
            "recent_delivery_count": len(dispatcher.recent_deliveries),
        },
        status_code=200,
    )


async def list_deliveries_endpoint(request: Request) -> JSONResponse:
    """Return recent accepted webhook deliveries and their LangGraph thread IDs."""
    dispatcher = get_webhook_dispatcher()
    return JSONResponse(
        {
            "status": "ok",
            "deliveries": dispatcher.recent_deliveries,
        },
        status_code=200,
    )


async def receive_webhook_endpoint(request: Request) -> JSONResponse:
    """Ingest, verify, normalize, and dispatch an incoming third-party webhook."""
    provider = str(request.path_params.get("provider") or "generic")
    raw_body = await request.body()
    headers = dict(request.headers)
    query_params = dict(request.query_params)

    dispatcher = get_webhook_dispatcher()
    status_code, payload = await dispatcher.handle_webhook(
        provider=provider,
        headers=headers,
        raw_body=raw_body,
        query_params=query_params,
    )
    return JSONResponse(payload, status_code=status_code)


def create_webhooks_app(dispatcher: WebhookDispatcher | None = None) -> Starlette:
    """Create the Starlette custom routes app for `langgraph.json` (`http.app`)."""
    if dispatcher is not None:
        set_webhook_dispatcher(dispatcher)

    routes = [
        Route("/webhooks", endpoint=list_webhooks_endpoint, methods=["GET"]),
        Route("/webhooks/deliveries", endpoint=list_deliveries_endpoint, methods=["GET"]),
        Route("/webhooks/{provider}", endpoint=receive_webhook_endpoint, methods=["POST"]),
    ]
    return Starlette(routes=routes)


app = create_webhooks_app()
