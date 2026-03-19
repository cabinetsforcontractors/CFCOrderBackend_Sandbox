"""
rate_limit.py
Shared slowapi Limiter instance.

Import `limiter` in any route module to apply per-endpoint rate limits:

    from rate_limit import limiter

    @router.get("/my-route")
    @limiter.limit("60/minute")
    async def my_route(request: Request, ...):
        ...

The limiter is wired into the FastAPI app in main.py:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

Default global limit: 200 requests/minute per IP.
Override per-route with @limiter.limit("N/period").
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
