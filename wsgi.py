"""
Production entry point for the toolroom app.

`python app.py`'s dev server isn't hardened for production traffic. This serves
the same Flask app through Waitress instead -- pure-Python, lightweight enough
for a Pi, no OS-level deps (unlike gunicorn, which doesn't play nice on Windows).

Usage:
    python wsgi.py

Normally run via toolroom.service (systemd) so it starts on boot and auto-restarts.

Threads default to 4 since we're backed by a single SQLite file -- more threads
just means more contention, not more throughput. Only raise WAITRESS_THREADS if
you've confirmed the DB isn't the bottleneck.
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
