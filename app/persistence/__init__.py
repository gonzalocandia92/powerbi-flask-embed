"""Persistence-level building blocks with no dependency on Flask, models or services.

Anything here can be imported by ``app.models``, by services and by Alembic-adjacent
tooling without creating import cycles.
"""
