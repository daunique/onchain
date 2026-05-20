"""
dashboard/app.py
────────────────
Flask REST API + dashboard server.
Uses absolute path for static folder to work regardless of
working directory (required for Render deployment).
"""

import os
from pathlib import Path
from flask import Flask, jsonify, send_file
from flask_cors import CORS
from data.store import store

# Absolute path — works regardless of cwd
STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")
CORS(app)


# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": "2.0"})


# ── Stats ─────────────────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    return jsonify(store.stats())


# ── Signals ───────────────────────────────────────────────────────────────────
@app.route("/api/signals")
def api_signals():
    return jsonify(store.get_signals(limit=100))


# ── Tokens ────────────────────────────────────────────────────────────────────
@app.route("/api/tokens")
def api_tokens():
    return jsonify(store.tokens)


# ── Pump events ───────────────────────────────────────────────────────────────
@app.route("/api/pump-events")
def api_pump_events():
    return jsonify(store.get_pump_events())


# ── Clusters ──────────────────────────────────────────────────────────────────
@app.route("/api/clusters")
def api_clusters():
    return jsonify(store.get_clusters())


# ── Watchlist ─────────────────────────────────────────────────────────────────
@app.route("/api/watchlist")
def api_watchlist():
    return jsonify(store.get_watchlist())


# ── Scan log ──────────────────────────────────────────────────────────────────
@app.route("/api/scan-log")
def api_scan_log():
    return jsonify(store.scan_log[-30:])


# ── Dashboard root ────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    index = STATIC_DIR / "index.html"
    return send_file(str(index))


# ── Catch-all for SPA (ignore api routes) ────────────────────────────────────
@app.route("/<path:path>")
def catch_all(path):
    if path.startswith("api/"):
        return jsonify({"error": "not found"}), 404
    index = STATIC_DIR / "index.html"
    return send_file(str(index))


if __name__ == "__main__":
    port = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "5000")))
    app.run(host="0.0.0.0", port=port, debug=False)
