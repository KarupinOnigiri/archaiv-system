from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# In-memory storage is fine for a single-process kiosk deployment (Pi or
# PC, one Flask process). If this ever moves to multiple worker processes
# behind a proxy, swap storage_uri for a shared backend (e.g. Redis) —
# in-memory limits are per-process and won't be shared across workers.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per minute"],
    storage_uri="memory://",
)
