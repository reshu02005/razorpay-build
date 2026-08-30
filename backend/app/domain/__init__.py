"""
The domain layer: vocabulary, wire shapes and failure modes.

Nothing in this package imports FastAPI, SQLAlchemy or any gateway SDK. That is
deliberate -- ``enums.py``, ``schemas.py`` and ``errors.py`` are the words every
other layer speaks, so they must not drag a framework along with them. It also
means the domain can be unit-tested with no database and no HTTP server running.
"""
