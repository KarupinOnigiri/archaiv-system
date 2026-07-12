from dotenv import load_dotenv
import os

root_dir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(root_dir, ".env"))

from flask import Flask

from routes.assets import assets_bp
from routes.members import members_bp
from routes.staff import staff_bp
from routes.auth import auth_bp
from routes.transactions import transactions_bp
from database import init_db
from services.seed_service import seed_initial_admin
from apscheduler.schedulers.background import BackgroundScheduler
from services.checkout_service import cleanup_stale_checkout_sessions

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY is not set in .env — required even though sessions are DB-backed, since Flask itself needs it for flash messages and any future signed cookies."
    )


app.register_blueprint(assets_bp)
app.register_blueprint(members_bp)
app.register_blueprint(staff_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(transactions_bp)

init_db()
seed_initial_admin()


scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_stale_checkout_sessions, "interval", minutes=5)

debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"

# Flask's debug reloader spawns a second process — only run the scheduler
# in the actual worker process, not the reloader's parent watcher, or
# you'll get every job firing twice.
if not debug_mode or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    scheduler.start()


@app.route("/")
def home():
    return "Flask is connected and running."


# if __name__ == "__main__":
#     app.run(debug=debug_mode)

if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", 5000))
    app.run(host=host, port=port, debug=debug_mode)
