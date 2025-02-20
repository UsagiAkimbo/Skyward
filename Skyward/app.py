# -*- coding: utf-8 -*-
import os
import logging
import requests
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')
CORS(app)  # Enable CORS if needed for cross-origin requests

# Set up rate limiting
limiter = Limiter(
    app,
    key_func=get_remote_address,
    default_limits=["100 per day", "20 per hour"]
)

# Secure API key for both YouTube and video update requests (set this on Railway)
API_KEY = os.environ.get('API_KEY', 'default_api_key')

# Whitelist of allowed video IDs – update with your pre-approved IDs.
ALLOWED_VIDEO_IDS = {"DEFAULT_VIDEO_ID", "SAFE_VIDEO_ID_1", "SAFE_VIDEO_ID_2"}

# Global variable to store the current approved video ID.
current_video_id = "DEFAULT_VIDEO_ID"  # This should be a safe default video.

@app.route('/')
@limiter.exempt  # Exempting static page requests from rate limiting if desired.
def index():
    # Serve the static HTML page that contains your embedded YouTube player.
    return send_from_directory('static', 'index.html')

@app.route('/get_next_video', methods=['GET'])
@limiter.limit("20 per minute")
def get_next_video():
    """
    Endpoint that the embedded player polls to get the currently approved video ID.
    """
    logger.info("GET /get_next_video requested")
    return jsonify({'videoId': current_video_id})

@app.route('/set_video', methods=['POST'])
@limiter.limit("10 per minute")
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
        logger.warning("Unauthorized attempt to update video. Invalid API key provided.")
        abort(403, description="Forbidden: Invalid API key")

    data = request.get_json()
    if not data or 'videoId' not in data:
        logger.warning("Bad Request: 'videoId' missing in payload.")
        abort(400, description="Bad Request: 'videoId' is required.")

    new_video_id = data['videoId']
    # Input validation: ensure video id looks like a typical YouTube id (11 characters, for example)
    if not isinstance(new_video_id, str) or len(new_video_id) != 11:
        logger.warning(f"Input validation failed for videoId: {new_video_id}")
        abort(400, description="Bad Request: Invalid videoId format.")

    # Check if the provided video ID is in our whitelist of allowed videos.
    if new_video_id not in ALLOWED_VIDEO_IDS:
        logger.warning(f"Attempt to set unapproved videoId: {new_video_id}")
        abort(400, description="Bad Request: Video not allowed.")

    global current_video_id
    current_video_id = new_video_id
    logger.info(f"Video updated to {new_video_id}")
    return jsonify({'status': 'success', 'videoId': current_video_id})

@app.route('/status', methods=['GET'])
@limiter.exempt
def status():
    logger.info("GET /status requested")
    return jsonify({"status": "ok", "message": "Server is running."})

# ---------------------------
# Proxy YouTube API Endpoints
# ---------------------------

@app.route('/youtube/search', methods=['GET'])
@limiter.limit("10 per minute")
def youtube_search():
    """
    Proxies a YouTube search API call.
    Expects query parameters:
      - q: the search query (required)
      - maxResults: number of results to return (optional, default 5)
    """
    query = request.args.get('q')
    if not query:
        abort(400, description="Missing query parameter 'q'.")

    try:
        max_results = int(request.args.get('maxResults', 5))
    except ValueError:
        abort(400, description="maxResults must be an integer.")

    youtube_api_url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "key": API_KEY,
        "q": query,
        "part": "snippet",
        "maxResults": max_results,
        "type": "video",
        "safeSearch": "strict"
    }
    logger.info(f"Proxying YouTube search for query: {query}, maxResults: {max_results}")
    response = requests.get(youtube_api_url, params=params)
    if response.status_code != 200:
        logger.error("YouTube API error: " + response.text)
        abort(response.status_code, description="YouTube API error.")
    return jsonify(response.json())

@app.route('/youtube/video', methods=['GET'])
@limiter.limit("10 per minute")
def youtube_video():
    """
    Proxies a YouTube video details API call.
    Expects query parameter:
      - videoId: the ID of the video (required)
    """
    video_id = request.args.get('videoId')
    if not video_id:
        abort(400, description="Missing videoId parameter.")

    # Input validation: check if videoId length is 11 characters
    if not isinstance(video_id, str) or len(video_id) != 11:
        abort(400, description="Invalid videoId format.")

    youtube_api_url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "key": API_KEY,
        "id": video_id,
        "part": "snippet,contentDetails,statistics"
    }
    logger.info(f"Proxying YouTube video details for videoId: {video_id}")
    response = requests.get(youtube_api_url, params=params)
    if response.status_code != 200:
        logger.error("YouTube API error: " + response.text)
        abort(response.status_code, description="YouTube API error.")
    return jsonify(response.json())

if __name__ == '__main__':
    # Railway provides the PORT environment variable. Default to 5000 if not set.
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
