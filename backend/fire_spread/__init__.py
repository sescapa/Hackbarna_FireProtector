"""Self-hosted ELMFIRE fire-spread pipeline for Catalonia.

Mounted by app/main.py as ``app.include_router(router, prefix="/fire")``; reads its own
environment (fire_spread.settings) so it stays mountable on its own.
"""

from .router import router, startup

__all__ = ["router", "startup"]
