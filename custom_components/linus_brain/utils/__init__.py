"""
Utils package for Linus Brain integration.

This package contains utility modules for:
- Area management and entity grouping
- Event listening and state changes
- Supabase HTTP client
- Rule engine for AI-generated automations
- Entity resolution for generic selectors
"""

from .app_storage import AppStorage
from .entity_resolver import EntityResolver

__all__ = ["AppStorage", "EntityResolver"]
