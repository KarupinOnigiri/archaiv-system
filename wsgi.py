"""Production entry point -- serves the Flask app through Waitress instead of the dev server.

Usage:
    python wsgi.py

Normally run via toolroom.service (systemd). Don't raise WAITRESS_THREADS without confirming the DB isn't the bottleneck.
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
