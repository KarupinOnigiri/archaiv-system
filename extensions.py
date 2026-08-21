from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# In-memory limits work for a single process; switch to Redis if you add workers.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per minute"],
    storage_uri="memory://",
)
