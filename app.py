"""CampusConnect HTTP API backed by Supabase PostgreSQL."""

import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from supabase import Client, create_client

load_dotenv()

app = Flask(__name__)
CORS(
    app,
    resources={r"/api/*": {"origins": "*"}},
    allow_headers="*",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)

_supabase: Client | None = None


def get_supabase() -> Client:
    """Create the Supabase client on first use so missing config has a JSON error."""
    global _supabase
    if _supabase is None:
        url = os.getenv("SUPABASE_URL")
        # The existing .env uses SUPABASE_PUBLISHABLE_KEY. SUPABASE_KEY is also supported.
        key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_PUBLISHABLE_KEY")
        if not url or not key:
            raise RuntimeError("Set SUPABASE_URL and SUPABASE_KEY in .env.")
        _supabase = create_client(url, key)
    return _supabase


def success(data, code: int = 200, message: str | None = None):
    body = {"data": data}
    if message:
        body["message"] = message
    return jsonify(body), code


def error(message: str, code: int):
    return jsonify({"error": {"message": message}}), code


def valid_uuid(value, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UUID string.")
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field} must be a valid UUID.") from exc


def require_json_object():
    if not request.is_json:
        raise ValueError("Content-Type must be application/json.")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
    return payload


def supabase_error_response(exc):
    """Keep database/client details out of API responses."""
    app.logger.exception("Supabase request failed", exc_info=exc)
    return error("Database request failed.", 503)


@app.get("/api/events")
def list_events():
    """Browse all events."""
    try:
        response = get_supabase().table("events").select("*").execute()
        return success(response.data or [])
    except RuntimeError as exc:
        return error(str(exc), 503)
    except Exception as exc:
        return supabase_error_response(exc)


@app.get("/api/events/<event_id>")
def get_event(event_id: str):
    """Return the event matching its UUID."""
    try:
        event_id = valid_uuid(event_id, "id")
    except ValueError as exc:
        return error(str(exc), 400)
    try:
        response = (
            get_supabase().table("events").select("*").eq("id", event_id).maybe_single().execute()
        )
        if response.data is None:
            return error("Event not found.", 404)
        return success(response.data)
    except RuntimeError as exc:
        return error(str(exc), 503)
    except Exception as exc:
        return supabase_error_response(exc)


@app.post("/api/registrations")
def create_registration():
    """Create a registration, returning 409 for a duplicate or full event."""
    try:
        payload = require_json_object()
        allowed_fields = {"user_id", "event_id"}
        if set(payload) != allowed_fields:
            raise ValueError("Request body must contain only user_id and event_id.")
        user_id = valid_uuid(payload.get("user_id"), "user_id")
        event_id = valid_uuid(payload.get("event_id"), "event_id")
    except ValueError as exc:
        return error(str(exc), 400)

    try:
        client = get_supabase()
        event_response = (
            client.table("events")
            .select("id, capacity")
            .eq("id", event_id)
            .maybe_single()
            .execute()
        )
        event = event_response.data
        if event is None:
            return error("Event not found.", 404)

        duplicate = (
            client.table("registrations")
            .select("id")
            .eq("user_id", user_id)
            .eq("event_id", event_id)
            .limit(1)
            .execute()
        )
        if duplicate.data:
            return error("User is already registered for this event.", 409)

        registrations = (
            client.table("registrations")
            .select("id", count="exact")
            .eq("event_id", event_id)
            .execute()
        )
        if registrations.count is not None and registrations.count >= event["capacity"]:
            return error("Event has reached capacity.", 409)

        # Supply the schema's UUID and timestamp fields explicitly.
        row = {
            "id": str(uuid4()),
            "user_id": user_id,
            "event_id": event_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        inserted = client.table("registrations").insert(row).execute()
        created = inserted.data[0] if inserted.data else row
        return success(created, 201, "Registration created.")
    except RuntimeError as exc:
        return error(str(exc), 503)
    except Exception as exc:
        # PostgreSQL unique-constraint violations are HTTP 409 through PostgREST.
        code = getattr(exc, "code", None)
        status_code = getattr(exc, "status_code", None)
        if code == "23505" or status_code == 409:
            return error("User is already registered for this event.", 409)
        return supabase_error_response(exc)


@app.get("/api/me/registrations")
def list_my_registrations():
    """List registrations by user UUID; identity is supplied because auth is omitted."""
    try:
        user_id = valid_uuid(request.args.get("user_id"), "user_id")
    except ValueError as exc:
        return error(str(exc), 400)
    try:
        response = (
            get_supabase().table("registrations").select("*").eq("user_id", user_id).execute()
        )
        return success(response.data or [])
    except RuntimeError as exc:
        return error(str(exc), 503)
    except Exception as exc:
        return supabase_error_response(exc)


@app.get("/api/events/<event_id>/registrations")
def list_event_registrations(event_id: str):
    """List users registered for an event. No authorization is applied."""
    try:
        event_id = valid_uuid(event_id, "id")
    except ValueError as exc:
        return error(str(exc), 400)
    try:
        client = get_supabase()
        event_response = (
            client.table("events").select("id").eq("id", event_id).maybe_single().execute()
        )
        if event_response.data is None:
            return error("Event not found.", 404)
        registration_response = (
            client.table("registrations").select("user_id").eq("event_id", event_id).execute()
        )
        user_ids = list({row["user_id"] for row in (registration_response.data or [])})
        if not user_ids:
            return success([])
        users_response = client.table("users").select("*").in_("id", user_ids).execute()
        return success(users_response.data or [])
    except RuntimeError as exc:
        return error(str(exc), 503)
    except Exception as exc:
        return supabase_error_response(exc)


@app.errorhandler(404)
def not_found(_exc):
    return error("Endpoint not found.", 404)


@app.errorhandler(405)
def method_not_allowed(_exc):
    return error("Method not allowed.", 405)


@app.errorhandler(500)
def internal_error(_exc):
    return error("Internal server error.", 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
