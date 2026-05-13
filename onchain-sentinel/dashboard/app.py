"""
dashboard/app.py
────────────────
Flask web server — serves the live dashboard and REST API.
"""

import os
from flask import Flask, jsonify, render_template_string, send_from_directory
from flask_cors import CORS
from data.store import store

app = Flask(__name__, static_folder="static")
CORS(app)


# ── REST API ──────────────────────────────────────────────────────────────────

@app.route("/api/signals")
def api_signals():
    return jsonify(store.get_all(limit=100))

@app.route("/api/signals/recent")
def api_signals_recent():
    return jsonify(store.get_last_24h())

@app.route("/api/stats")
def api_stats():
    return jsonify(store.summary_stats())

@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "token": os.getenv("TOKEN_SYMBOL", "TOKEN")})


# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = open(os.path.join(os.path.dirname(__file__), "static", "index.html")).read()

@app.route("/")
def dashboard():
    return DASHBOARD_HTML


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
