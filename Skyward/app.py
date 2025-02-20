# -*- coding: utf-8 -*-
import os
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS

app = Flask(__name__, static_folder='static')
CORS(app)  # Enable CORS if needed for cross-origin requests

# Secure API key for video update requests (set this on Railway)
API_KEY = os.environ.get('API_KEY', 'default_api_key')

# Whitelist of allowed video IDs – update with your pre-approved IDs.
ALLOWED_VIDEO_IDS = {"DEFAULT_VIDEO_ID", "SAFE_VIDEO_ID_1", "SAFE_VIDEO_ID_2"}

# Global variable to store the current approved video ID.
current_video_id = "DEFAULT_VIDEO_ID"  # This should be a safe default video.

@app.route('/')
def index():
    # Serve the static HTML page that contains your embedded YouTube player.
    return send_from_directory('static', 'index.html')

@app.route('/get_next_video', methods=['GET'])
def get_next_video():
    """
    Endpoint that the embedded player polls to get the currently approved video ID.
    """
    return jsonify({'videoId': current_video_id})

@app.route('/set_video', methods=['POST'])
def set_video():
    """
    Secured endpoint to update the current video.
    Expects a JSON payload: { "videoId": "NEW_VIDEO_ID" }
    This endpoint is protected by an API key (sent in the header "X-API-Key")
    and validates that the new video is in the allowed whitelist.
    """
    # Validate API key
    provided_key = request.headers.get('X-API-Key')
    if provided_key != API_KEY:
        abort(403, description="Forbidden: Invalid API key")

    data = request.get_json()
    if not data or 'videoId' not in data:
        abort(400, description="Bad Request: 'videoId' is required.")

    new_video_id = data['videoId']
    # Check if the provided video ID is in our whitelist of allowed videos.
    if new_video_id not in ALLOWED_VIDEO_IDS:
        abort(400, description="Bad Request: Video not allowed.")

    global current_video_id
    current_video_id = new_video_id
    return jsonify({'status': 'success', 'videoId': current_video_id})

@app.route('/status', methods=['GET'])
def status():
    return jsonify({"status": "ok", "message": "Server is running."})

if __name__ == '__main__':
    # Railway provides the PORT environment variable. Default to 5000 if not set.
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
