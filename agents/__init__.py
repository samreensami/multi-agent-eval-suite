"""
agents/__init__.py — Agent Registry

Spec Reference: docs/spec.md#2-system-architecture
All agents in this package implement the agent contract defined in docs/spec.md#5.
"""

from agents.orchestrator import Orchestrator

__all__ = ["Orchestrator"]
__version__ = "1.0.0"
