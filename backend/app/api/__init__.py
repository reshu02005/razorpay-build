"""
The HTTP layer.

Everything in this package is a *translator*: it turns an HTTP request into a
call on a service object and turns the result into one of the Pydantic schemas
from ``app.domain.schemas``. It holds no business rules, no ORM queries against
the domain tables and no guardrail logic.

Why the layer is kept this thin: the interesting behaviour of RecoverAI -- the
guardrail engine, the state machine, the hash-chained ledger -- has to be
testable without spinning up a web server. If a policy decision lived in a route
handler, the only way to test it would be through an HTTP client, and the test
suite would be measuring FastAPI as much as it measures the product.

This module is deliberately import-free. ``app.main`` imports
``app.api.routers``; keeping the package ``__init__`` empty of imports means
there is no path by which importing the package can pull in the database, the
gateway or the ML model as a side effect.
"""
