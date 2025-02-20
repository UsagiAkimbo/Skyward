# -*- coding: utf-8 -*-
import os
import logging
import requests
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Additional imports for database and scheduling.
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')
CORS(app)

# -----------------------
# Rate Limiting Setup
# -----------------------
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["100 per day", "20 per hour"]
)
limiter.init_app(app)

# -----------------------
# Database Setup
# -----------------------
# Use the same database as Mirror. Ensure DATABASE_URL is set in your environment.
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///Database.sqlite')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# -----------------------
# Models for Talent Management
# -----------------------
class ApprovedTalent(db.Model):
    __tablename__ = 'approved_talents'
    id = db.Column(db.Integer, primary_key=True)
    talent_name = db.Column(db.String(128), nullable=False)
    channel_id = db.Column(db.String(64), nullable=False)
    # Additional fields can be added as needed.

class TalentVideo(db.Model):
    __tablename__ = 'talent_videos'
    id = db.Column(db.Integer, primary_key=True)
    talent_id = db.Column(db.Integer, db.ForeignKey('approved_talents.id'), nullable=False)
    video_id = db.Column(db.String(64), nullable=False, unique=True)
    published_at = db.Column(db.String(64))
    title = db.Column(db.String(256))
    # Additional fields (description, thumbnail URL, etc.) can be added if desired.

# If you manage schema via migrations in Mirror's system, you can remove the following line.
with app.app_context():
    db.create_all()

# -----------------------
# Existing Endpoints
# -----------------------
@app.route('/')
@limiter.exempt
def index():
    return send_from_directory('static', 'index.html')

@app.route('/get_next_video', methods=['GET'])
@limiter.limit("20 per minute")
def get_next_video():
    # This endpoint remains as a placeholder or can be modified to return a global approved video.
    return jsonify({'videoId': "DEFAULT_VIDEO_ID"})

@app.route('/set_video', methods=['POST'])
@limiter.limit("10 per minute")
def set_video():
    provided_key = request.headers.get('X-API-Key')
    if provided_key != os.environ.get('API_KEY', 'default_api_key'):
        logger.warning("Invalid API key provided for /set_video.")
        abort(403, description="Forbidden: Invalid API key")
    data = request.get_json()
    if not data or 'videoId' not in data:
        logger.warning("Bad Request: 'videoId' missing in payload for /set_video.")
        abort(400, description="Bad Request: 'videoId' is required.")
    new_video_id = data['videoId']
    logger.info(f"Approved video updated to {new_video_id}")
    return jsonify({'status': 'success', 'videoId': new_video_id})

@app.route('/status', methods=['GET'])
@limiter.exempt
def status():
    logger.info("GET /status requested")
    return jsonify({"status": "ok", "message": "Server is running."})

# -----------------------
# Proxy Endpoints (YouTube API)
# -----------------------
@app.route('/youtube/search', methods=['GET'])
@limiter.limit("10 per minute")
def youtube_search():
    query = request.args.get('q')
    if not query:
        abort(400, description="Missing query parameter 'q'.")
    try:
        max_results = int(request.args.get('maxResults', 5))
    except ValueError:
        abort(400, description="maxResults must be an integer.")
    youtube_api_url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "key": os.environ.get('API_KEY', 'default_api_key'),
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
    video_id = request.args.get('videoId')
    if not video_id:
        abort(400, description="Missing videoId parameter.")
    if not isinstance(video_id, str) or len(video_id) != 11:
        abort(400, description="Invalid videoId format.")
    youtube_api_url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "key": os.environ.get('API_KEY', 'default_api_key'),
        "id": video_id,
        "part": "snippet,contentDetails,statistics"
    }
    logger.info(f"Proxying YouTube video details for videoId: {video_id}")
    response = requests.get(youtube_api_url, params=params)
    if response.status_code != 200:
        logger.error("YouTube API error: " + response.text)
        abort(response.status_code, description="YouTube API error.")
    return jsonify(response.json())

# -----------------------
# Talent Video Manager Logic (Backend)
# -----------------------

def update_talent_videos():
    logger.info("Running talent video update job...")
    talents = ApprovedTalent.query.all()
    for talent in talents:
        youtube_api_url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "key": os.environ.get('API_KEY', 'default_api_key'),
            "channelId": talent.channel_id,
            "part": "snippet",
            "order": "date",
            "type": "video",
            "maxResults": 10
        }
        response = requests.get(youtube_api_url, params=params)
        if response.status_code == 200:
            data = response.json()
            for item in data.get("items", []):
                video_id = item["id"]["videoId"]
                # Check if the video already exists.
                existing = TalentVideo.query.filter_by(video_id=video_id).first()
                if not existing:
                    new_video = TalentVideo(
                        talent_id=talent.id,
                        video_id=video_id,
                        published_at=item["snippet"].get("publishedAt", ""),
                        title=item["snippet"].get("title", "")
                    )
                    db.session.add(new_video)
            db.session.commit()
            logger.info(f"Updated videos for talent: {talent.talent_name}")
        else:
            logger.error(f"Error fetching videos for {talent.talent_name}: {response.text}")

@app.route('/talent_videos', methods=['GET'])
@limiter.limit("10 per minute")
def get_talent_videos():
    talents = ApprovedTalent.query.all()
    result = []
    for talent in talents:
        videos = TalentVideo.query.filter_by(talent_id=talent.id).all()
        video_list = [{"video_id": video.video_id, "published_at": video.published_at, "title": video.title} for video in videos]
        result.append({
            "talent_name": talent.talent_name,
            "channel_id": talent.channel_id,
            "videos": video_list
        })
    return jsonify(result)

# -----------------------
# Scheduler Setup
# -----------------------
scheduler = BackgroundScheduler()
# Run the update job every 10 minutes (adjust as needed)
scheduler.add_job(func=update_talent_videos, trigger="interval", minutes=10)
scheduler.start()

# -----------------------
# Application Entry Point
# -----------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
