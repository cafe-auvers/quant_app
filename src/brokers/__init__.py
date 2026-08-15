"""Broker-facing protocol definitions for the execution gateway (Workstream 3).

Kept separate from :mod:`src.services` because these are pure interfaces --
no SQLAlchemy, no Qt, no application state -- meant to be imported by both
the gateway and any future broker adapter without dragging in the rest of
the services layer.
"""
