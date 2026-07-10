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

app = Flask(__name__)

app.register_blueprint(assets_bp)
app.register_blueprint(members_bp)
app.register_blueprint(staff_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(transactions_bp)

init_db()
seed_initial_admin()


@app.route("/")
def home():
    return "Flask is connected and running."


debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"

if __name__ == "__main__":
    app.run(debug=debug_mode)
