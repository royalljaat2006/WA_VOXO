"""Demo-only HTTP API, for showing the pipeline without WhatsApp.

Not part of the product. See `api.py` for why it exists and why it is off by
default.
"""

from .api import DemoDeps, create_demo_router

__all__ = ["DemoDeps", "create_demo_router"]
