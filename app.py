import os
import secrets
import requests
from flask import Flask, redirect, request, session, url_for, render_template, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))

ESI_CLIENT_ID = os.getenv("ESI_CLIENT_ID", "")
ESI_CLIENT_SECRET = os.getenv("ESI_CLIENT_SECRET", "")
ESI_CALLBACK_URL = os.getenv("ESI_CALLBACK_URL", "http://localhost:5000/callback")

ESI_BASE = "https://esi.evetech.net/latest"
ESI_AUTH = "https://login.eveonline.com/v2/oauth"
ZKILL_BASE = "https://zkillboard.com/api"

ESI_SCOPES = "esi-killmails.read_killmails.v1 esi-killmails.read_corporation_killmails.v1"


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/login")
def login():
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    params = {
        "response_type": "code",
        "redirect_uri": ESI_CALLBACK_URL,
        "client_id": ESI_CLIENT_ID,
        "scope": ESI_SCOPES,
        "state": state,
    }
    url = requests.Request("GET", f"{ESI_AUTH}/authorize", params=params).prepare().url
    return redirect(url)


@app.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        return render_template("error.html", message=f"ESI auth error: {error}"), 400

    state = request.args.get("state")
    if state != session.pop("oauth_state", None):
        return render_template("error.html", message="Invalid OAuth state."), 403

    code = request.args.get("code")
    token_resp = requests.post(
        f"{ESI_AUTH}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": ESI_CALLBACK_URL,
        },
        auth=(ESI_CLIENT_ID, ESI_CLIENT_SECRET),
    )
    token_resp.raise_for_status()
    tokens = token_resp.json()

    verify_resp = requests.get(
        f"{ESI_AUTH}/verify",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    verify_resp.raise_for_status()
    identity = verify_resp.json()

    session["access_token"] = tokens["access_token"]
    session["character_id"] = identity["CharacterID"]
    session["character_name"] = identity["CharacterName"]

    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", character_name=session.get("character_name"))


@app.route("/dashboard")
def dashboard():
    if "character_id" not in session:
        return redirect(url_for("login"))
    character_id = session["character_id"]
    kills = _fetch_kills(character_id)
    stats = _compute_stats(kills)
    return render_template(
        "dashboard.html",
        character_name=session["character_name"],
        kills=kills[:25],
        stats=stats,
    )


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/kills")
def api_kills():
    if "character_id" not in session:
        return jsonify({"error": "not authenticated"}), 401
    kills = _fetch_kills(session["character_id"])
    return jsonify(kills)


@app.route("/api/stats")
def api_stats():
    if "character_id" not in session:
        return jsonify({"error": "not authenticated"}), 401
    kills = _fetch_kills(session["character_id"])
    return jsonify(_compute_stats(kills))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_kills(character_id: int) -> list[dict]:
    """Pull up to 200 recent kills from zKillboard for the character."""
    url = f"{ZKILL_BASE}/characterID/{character_id}/"
    try:
        resp = requests.get(url, headers={"Accept-Encoding": "gzip"}, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
    except Exception:
        return []

    kills = []
    for entry in raw[:200]:
        km = entry.get("killmail", {})
        zkb = entry.get("zkb", {})
        victim = km.get("victim", {})
        kills.append({
            "killmail_id": km.get("killmail_id"),
            "killmail_time": km.get("killmail_time", ""),
            "solar_system_id": km.get("solar_system_id"),
            "ship_type_id": victim.get("ship_type_id"),
            "victim_character_id": victim.get("character_id"),
            "victim_corporation_id": victim.get("corporation_id"),
            "total_value": zkb.get("totalValue", 0),
            "points": zkb.get("points", 0),
            "is_solo": zkb.get("solo", False),
            "is_npc": zkb.get("npc", False),
        })
    return kills


def _compute_stats(kills: list[dict]) -> dict:
    total_isk = sum(k["total_value"] for k in kills)
    solo_kills = sum(1 for k in kills if k["is_solo"])
    npc_kills = sum(1 for k in kills if k["is_npc"])
    return {
        "total_kills": len(kills),
        "total_isk_destroyed": total_isk,
        "total_isk_destroyed_formatted": _fmt_isk(total_isk),
        "solo_kills": solo_kills,
        "npc_kills": npc_kills,
        "pvp_kills": len(kills) - npc_kills,
    }


def _fmt_isk(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B ISK"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M ISK"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K ISK"
    return f"{value:.0f} ISK"


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "true").lower() == "true"
    app.run(host=host, port=port, debug=debug)
