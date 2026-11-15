"""
Flask routes for Tiger Data analytics. Registered by koov/api_server.py.

  GET /api/tiger/status                      connection + schema + writer health
  GET /api/meetings/<meeting_id>/analytics   talk share, timeline, topics, decisions over time, latency
        ?bucket=1 minute                     any Postgres interval: "30 seconds", "5 minutes", ...
  GET /api/analytics/overview?days=7         cross-meeting stats (continuous aggregate)
  GET /api/tiger/search?q=pricing            full-text search across all meeting transcripts
        &meeting_id=...&days=30&limit=50

KOOV_API_KEY auth (if set) applies automatically via the app's before_request hook.
"""

from __future__ import annotations

import logging
import re

from flask import Blueprint, jsonify, request

from koov import tiger_store

log = logging.getLogger("koov.tiger_api")

tiger_bp = Blueprint("tiger", __name__)

_INTERVAL_RE = re.compile(r"^\s*\d{1,4}\s*(second|seconds|minute|minutes|hour|hours|day|days)\s*$", re.I)
_MEETING_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,200}$")


def _disabled():
    return jsonify({
        "success": False,
        "error": "Tiger Data is not configured. Set TIGER_DATABASE_URL in .env (see docs/TIGER_DATA.md).",
    }), 503


@tiger_bp.route("/api/tiger/status", methods=["GET"])
def tiger_status():
    return jsonify({"success": True, **tiger_store.health()})


@tiger_bp.route("/api/meetings/<meeting_id>/analytics", methods=["GET"])
def meeting_analytics(meeting_id: str):
    if not tiger_store.is_enabled():
        return _disabled()
    if not _MEETING_ID_RE.match(meeting_id):
        return jsonify({"success": False, "error": "Invalid meeting_id"}), 400
    bucket = request.args.get("bucket", "1 minute")
    if not _INTERVAL_RE.match(bucket):
        return jsonify({"success": False, "error": "bucket must look like '30 seconds' or '5 minutes'"}), 400
    try:
        return jsonify({"success": True, **tiger_store.meeting_analytics(meeting_id, bucket)})
    except Exception as exc:
        log.exception("meeting analytics failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@tiger_bp.route("/api/analytics/overview", methods=["GET"])
def analytics_overview():
    if not tiger_store.is_enabled():
        return _disabled()
    try:
        days = int(request.args.get("days", "7"))
    except ValueError:
        days = 7
    try:
        return jsonify({"success": True, **tiger_store.overview(days)})
    except Exception as exc:
        log.exception("analytics overview failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@tiger_bp.route("/api/tiger/search", methods=["GET"])
def transcript_search():
    if not tiger_store.is_enabled():
        return _disabled()
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"success": False, "error": "q is required"}), 400
    meeting_id = request.args.get("meeting_id") or None
    if meeting_id and not _MEETING_ID_RE.match(meeting_id):
        return jsonify({"success": False, "error": "Invalid meeting_id"}), 400
    try:
        days = int(request.args.get("days", "30"))
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        return jsonify({"success": False, "error": "days and limit must be integers"}), 400
    try:
        results = tiger_store.search(q, meeting_id=meeting_id, days=days, limit=limit)
        return jsonify({"success": True, "query": q, "count": len(results), "results": results})
    except Exception as exc:
        log.exception("transcript search failed")
        return jsonify({"success": False, "error": str(exc)}), 500
