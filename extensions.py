from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# In-memory storage is fine for single-process deployment; switch to Redis if you ever
# add multiple workers, since in-memory limits don't get shared across processes.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per minute"],
    storage_uri="memory://",
)
