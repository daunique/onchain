"""
dashboard/app.py
────────────────
Flask REST API + dashboard server.
All endpoints consumed by the live dashboard frontend.
"""

import os
from flask import Flask, jsonify
from flask_cors import CORS
from data.store import store

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)


# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "2.0"})


# ── Stats ─────────────────────────────────────────────────────────────────────
@app.route("/api/stats")
def stats():
    return jsonify(store.stats())


# ── Signals ───────────────────────────────────────────────────────────────────
@app.route("/api/signals")
def signals():
    return jsonify(store.get_signals(limit=100))


# ── Tokens ────────────────────────────────────────────────────────────────────
@app.route("/api/tokens")
def tokens():
    return jsonify(store.tokens)


# ── Pump events ───────────────────────────────────────────────────────────────
@app.route("/api/pump-events")
def pump_events():
    return jsonify(store.get_pump_events())


# ── Clusters ──────────────────────────────────────────────────────────────────
@app.route("/api/clusters")
def clusters():
    return jsonify(store.get_clusters())


# ── Watchlist ─────────────────────────────────────────────────────────────────
@app.route("/api/watchlist")
def watchlist():
    return jsonify(store.get_watchlist())


# ── Scan log ──────────────────────────────────────────────────────────────────
@app.route("/api/scan-log")
def scan_log():
    return jsonify(store.scan_log[-30:])


# ── Dashboard (catch-all → index.html) ───────────────────────────────────────
@app.route("/")
@app.route("/<path:path>")
def dashboard(path=""):
    return app.send_static_file("index.html")


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
