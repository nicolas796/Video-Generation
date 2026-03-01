"""Lightweight stub for rembg used in tests.

This stub provides a no-op remove() implementation so the application can
import rembg without pulling the full dependency stack (which lacks Python
3.14 wheels).
"""
from __future__ import annotations
from typing import Any

def remove(image: Any, *_, **__):
    """Return the input image unchanged."""
    return image
