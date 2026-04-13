"""
Flask server for the AI Mock Interview frontend.

This server:
1. Serves the HTML/JS frontend at http://localhost:5000
2. Provides a /token endpoint that generates LiveKit access tokens
3. The frontend uses LiveKit JS SDK to connect to the room with voice

HOW TO RUN:
  Terminal 1: python agent.py dev          (starts the AI interviewer)
  Terminal 2: python server.py             (starts the web frontend)
  Then open:  http://localhost:5000
"""

import os
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from livekit import api

load_dotenv()

app = Flask(__name__)

LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")


@app.route("/")
def index():
    """Serve the main interview page."""
    return render_template("index.html", livekit_url=LIVEKIT_URL)


@app.route("/token", methods=["POST"])
def get_token():
    """Generate a LiveKit access token for the user to join a room."""
    data = request.json or {}
    participant_name = data.get("name", "Candidate")

    # Create a unique room for each interview session
    room_name = f"interview-{uuid.uuid4().hex[:8]}"

    token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    token.with_identity(f"user-{uuid.uuid4().hex[:6]}")
    token.with_name(participant_name)
    token.with_grants(
        api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True,
        )
    )

    return jsonify({
        "token": token.to_jwt(),
        "room": room_name,
        "url": LIVEKIT_URL,
    })


if __name__ == "__main__":
    print("\n🎤 Mock Interview Frontend running at: http://localhost:5000\n")
    print("Make sure your agent is running in another terminal:")
    print("  python agent.py dev\n")
    app.run(debug=True, port=5000)