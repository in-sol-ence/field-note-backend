"""Compatible subset of social-signals for Fieldnotes T1 (HN + Reddit).

Implements ``GET /health`` and ``POST /v1/jobs/watch`` + poll so
``field-note-backend.harvest`` can scrape live without the private
``social-signals`` monorepo.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
