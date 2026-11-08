"""Responses HTTP routes sharing the existing inference and cancellation backend."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
import json

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from responses_support import MAX_RESPONSE_BYTES, RequestError, ResponseEvents, ResponseStore, error_body, prepare_request


def create_router(backend):
    router = APIRouter()
    store = ResponseStore()

    def error(exc):
        return JSONResponse(status_code=exc.status, content=error_body(str(exc), param=exc.param, code=exc.code))

    @router.get("/v1/responses/{response_id}")
    async def retrieve(response_id: str):
        try:
            return store.get(response_id)["response"]
        except RequestError as exc:
            return error(exc)

    @router.delete("/v1/responses/{response_id}")
    async def delete(response_id: str):
        try:
            store.delete(response_id)
            return {"id": response_id, "object": "response.deleted", "deleted": True}
        except RequestError as exc:
            return error(exc)

    @router.post("/v1/responses")
    async def create(request: Request):
        try:
            raw = await request.body()
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RequestError("Request body exceeds 16 MiB", status=413)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise RequestError("Invalid JSON body") from None
            if not isinstance(body, dict):
                raise RequestError("Expected a JSON object")
            if "model" in body and (not isinstance(body["model"], str) or not body["model"].strip()):
                raise RequestError("Expected a model name string", "model")
            try:
                body["model"] = backend.validate_model(body.get("model"))
            except backend.ModelNotSupported as exc:
                raise RequestError(f"Model '{exc}' is not supported", "model", status=404, code="model_not_found") from None
            prior_id = body.get("previous_response_id")
            if prior_id is not None and not isinstance(prior_id, str):
                raise RequestError("Expected a response ID string", "previous_response_id")
            prior = store.get(prior_id) if prior_id else None
            payload, items, registry = prepare_request(body, backend, prior)
            headers = backend.build_headers()
        except RequestError as exc:
            return error(exc)
        except backend.ConfigError as exc:
            return JSONResponse(status_code=503, content=error_body(str(exc), kind="server_error"))
        state = ResponseEvents(body, registry)
        backend._dbg("responses_start", response_id=state.response["id"], stream=body.get("stream", False),
                     reasoning_effort=payload.get("reasoning_effort"), tools=len(payload.get("tools") or []),
                     messages=len(payload["messages"]), previous_response_id=prior_id, store=body.get("store", True))

        async def events():
            for event in state.start():
                yield event
            try:
                async with aclosing(backend.iter_events(payload, headers, buffered=not body.get("stream", False))) as source:
                    async for value in source:
                        if value["type"] == "error":
                            backend._dbg("responses_failed", response_id=state.response["id"], error=value["message"])
                            yield state.fail(value["message"], "upstream_error")
                            return
                        if value["type"] == "ping":
                            yield None  # SSE comment; no invented Responses event type.
                            continue
                        for event in state.feed(value):
                            yield event
                final = state.finish()
                if body.get("store", True):
                    store.put(state.response, items, registry)
                for event in final:
                    yield event
                backend._dbg("responses_done", response_id=state.response["id"], status=state.response["status"],
                             usage=state.response["usage"], output_items=len(state.response["output"]))
            except (ValueError, httpx.HTTPError) as exc:
                backend._dbg("responses_failed", response_id=state.response["id"], error=str(exc)[:1500])
                yield state.fail(str(exc), "upstream_error")

        async def stream():
            async with aclosing(events()) as source:
                async for event in source:
                    if event is None:
                        yield ": ping\n\n"
                    else:
                        yield backend.sse(event["type"], event)

        if body.get("stream", False):
            return backend.DisconnectStreamingResponse(stream(), media_type="text/event-stream", headers={
                "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

        async def aggregate():
            async with aclosing(events()) as source:
                async for _ in source:
                    pass
            return state.response

        generation = asyncio.create_task(aggregate())
        disconnected = asyncio.create_task(backend._disconnected(request))
        try:
            completed, _ = await asyncio.wait((generation, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if generation not in completed:
                return JSONResponse(status_code=499, content=error_body("Client disconnected", kind="server_error"))
            response = await generation
            if response["status"] == "failed":
                return JSONResponse(status_code=502, content=error_body(response["error"]["message"], kind="server_error",
                                                                      code=response["error"]["code"]))
            return JSONResponse(content=response)
        finally:
            generation.cancel()
            disconnected.cancel()
            await asyncio.gather(generation, disconnected, return_exceptions=True)

    return router
