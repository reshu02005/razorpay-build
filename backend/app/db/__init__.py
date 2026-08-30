"""
Persistence: the SQLAlchemy engine, the ORM models and the seed data.

``base.py`` owns the engine and the session factory, ``models.py`` owns the
tables, and ``scenarios.py`` owns the catalogue of realistic Razorpay failures
used to populate a fresh database. Keeping the scenario catalogue here rather
than inside the agent package matters: it is test *input*, not domain logic, and
the classifier must never be able to see the answer key it is graded against.
"""
