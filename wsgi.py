"""
Production entry point for the toolroom app.

The dev server started by `python app.py` (Werkzeug's built-in server) is
not meant to run unattended — it isn't hardened for production traffic and
prints a startup warning to that effect. This file serves the same Flask
app through Waitress instead, which is a pure-Python WSGI server that's
lightweight enough for a Raspberry Pi and doesn't need any OS-level
dependencies (unlike gunicorn, which relies on features not available on
Windows and is less common in single-process Pi deployments).

Usage:
    python wsgi.py

Typically you won't run this by hand — see toolroom.service, which runs
this under systemd so it starts on boot and restarts automatically if it
ever crashes.

Threads: kept modest (default 4) since this app is backed by a single
SQLite file — more threads just means more contention for the same
database, not more real throughput. Raise WAITRESS_THREADS in .env only
if you've confirmed the DB isn't the bottleneck.
"""

import os
from waitress import serve
from app import app

if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", 5000))
    threads = int(os.environ.get("WAITRESS_THREADS", 4))

    print(f"Serving toolroom app on http://{host}:{port} (waitress, {threads} threads)")
    serve(app, host=host, port=port, threads=threads)
