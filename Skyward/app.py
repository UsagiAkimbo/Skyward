# -*- coding: utf-8 -*-
import os
import logging
import requests
import sqlite3  # Added for dump_db endpoint
import json
import struct  # Added for binary decoding
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from google.cloud import secretmanager
from google.oauth2 import service_account

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)
logger.info("Current API_KEY: " + os.environ.get("API_KEY", "Not Found"))

# SQLite path on Railway (defined early for use in credential functions)
DB_PATH = '/app/Database.sqlite'

# Path to the binary protocol file (renamed to garbage.bin)
GARBAGE_BIN_PATH = os.path.join(os.path.dirname(__file__), 'garbage.bin')

# Database connection function for SQLite
def get_db_connection():
    try:
        conn = sqlite3.connect(DB_PATH)
        logger.info("Database connection established")
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to database: {str(e)}")
        raise

# Generalized function to get a secret ARN from the database
def get_secret_arn_from_db(secret_name):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT secret_arn FROM secrets WHERE secret_name = ?", (secret_name,))
        result = cursor.fetchone()
        conn.close()
        if result:
            logger.info(f"Found secret ARN for {secret_name}: {result[0]}")
            return result[0]
        else:
            logger.error(f"No secret ARN found for {secret_name}")
            return None
    except Exception as e:
        logger.error(f"Error querying secret ARN for {secret_name}: {str(e)}")
        return None

# Retrieve a secret from Google Cloud Secret Manager with explicit credentials
def get_secret(secret_name, credentials=None):
    try:
        client = secretmanager.SecretManagerServiceClient(credentials=credentials)
        logger.info(f"Using provided credentials for {secret_name}")
        secret_arn = get_secret_arn_from_db(secret_name)
        if not secret_arn:
            logger.error(f"No secret ARN available for {secret_name}")
            return None
        response = client.access_secret_version(name=f"{secret_arn}/versions/latest")
        secret_value = response.payload.data.decode("UTF-8")
        logger.info(f"Successfully retrieved {secret_name} from Secret Manager")
        return secret_value
    except Exception as e:
        logger.error(f"Failed to retrieve {secret_name} from Secret Manager: {str(e)}")
        return None

# Decode the binary protocol file (garbage.bin) to get bootstrap credentials
def decode_binary_to_json(binary_data):
    try:
        message_type = struct.unpack('B', binary_data[0:1])[0]
        if message_type != 0xAA:  # Check for the fake message type used in encoding
            logger.error("Invalid message type in garbage.bin")
            return None
        length = struct.unpack('>I', binary_data[1:5])[0]
        body = binary_data[5:5+length]
        json_str = ''.join(chr(b ^ 0x5A) for b in body)  # Reverse XOR obfuscation
        return json_str
    except Exception as e:
        logger.error(f"Failed to decode garbage.bin: {str(e)}")
        return None

# Set up Google Cloud credentials at startup using garbage.bin
def setup_credentials():
    global credentials
    try:
        if not os.path.exists(GARBAGE_BIN_PATH):
            logger.error(f"garbage.bin not found at {GARBAGE_BIN_PATH}")
            return None
        
        # Read and decode garbage.bin
        with open(GARBAGE_BIN_PATH, 'rb') as f:
            binary_data = f.read()
        creds_json = decode_binary_to_json(binary_data)
        if not creds_json:
            logger.error("Failed to decode credentials from garbage.bin")
            return None
        
        # Use decoded credentials to bootstrap Secret Manager access
        bootstrap_creds_dict = json.loads(creds_json)
        bootstrap_credentials = service_account.Credentials.from_service_account_info(bootstrap_creds_dict)
        
        # Fetch the real credentials from Secret Manager
        real_creds_json = get_secret('google_oauth_cred', bootstrap_credentials)
        if not real_creds_json:
            logger.error("Could not retrieve google_oauth_cred from Secret Manager")
            return None
        
        # Set up the real credentials for subsequent use
        real_creds_dict = json.loads(real_creds_json)
        credentials = service_account.Credentials.from_service_account_info(real_creds_dict)
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '/tmp/credentials.json'
        with open('/tmp/credentials.json', 'w') as f:
            json.dump(real_creds_dict, f)
        logger.info("Google Cloud credentials set up successfully using garbage.bin bootstrap")
        return credentials
    except Exception as e:
        logger.error(f"Error setting up Google Cloud credentials: {str(e)}")
        return None

# Retrieve the YouTube API key
def get_api_key():
    return get_secret('youtube_api_key', credentials)

# Initialize Flask app
app = Flask(__name__, static_folder='static')
logger.info("Starting Flask app initialization")
credentials = None  # Initialize globally
credentials = setup_credentials()  # Set up credentials at startup
print("Environment variables at startup:", os.environ)
CORS(app)

# Rate Limiting Setup
limiter = Limiter(key_func=get_remote_address, default_limits=["100 per day", "20 per hour"])
limiter.init_app(app)

app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
logger.info(f"Checking database file at startup: {os.path.exists(DB_PATH)}")
try:
    db = SQLAlchemy(app)
    logger.info("SQLAlchemy initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize SQLAlchemy: {str(e)}")

# Models for Talent Management
class ApprovedTalent(db.Model):
    __tablename__ = 'approved_talents'
    id = db.Column(db.Integer, primary_key=True)
    talent_name = db.Column(db.String(128), nullable=False)
    channel_id = db.Column(db.String(64), nullable=False)

class TalentVideo(db.Model):
    __tablename__ = 'talent_videos'
    id = db.Column(db.Integer, primary_key=True)
    talent_id = db.Column(db.Integer, db.ForeignKey('approved_talents.id'), nullable=False)
    video_id = db.Column(db.String(64), nullable=False, unique=True)
    published_at = db.Column(db.String(64))
    title = db.Column(db.String(256))

@app.route('/')
@limiter.exempt
def index():
    var1 = os.environ.get('VAR1', 'default')
    return f'VAR1 is {var1}'

@app.route('/get_next_video', methods=['GET'])
@limiter.limit("20 per minute")
def get_next_video():
    latest_video = TalentVideo.query.order_by(TalentVideo.published_at.desc()).first()
    if latest_video:
        return jsonify({'videoId': latest_video.video_id})
    return jsonify({'videoId': "DEFAULT_VIDEO_ID"})

@app.route('/set_video', methods=['POST'])
@limiter.limit("10 per minute")
def set_video():
    provided_key = request.headers.get('X-API-Key')
    api_key = get_api_key()
    if not api_key:
        logger.error("API key retrieval failed for /set_video")
        abort(500, description="Internal server error: API key unavailable")
    if provided_key != api_key:
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
    api_key = get_api_key()  # Already uses global credentials
    if not api_key:
        abort(500, description="Failed to retrieve API key")
    params = {
        "key": api_key,
        "q": query,
        "part": "snippet",
        "maxResults": max_results,
        "type": "video",
        "safeSearch": "strict"
    }
    logger.info(f"Proxying YouTube search for query: {query}, maxResults: {max_results}, using API key")
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
    api_key = get_api_key()  # Already uses global credentials
    if not api_key:
        abort(500, description="Failed to retrieve API key")
    params = {
        "key": api_key,
        "id": video_id,
        "part": "snippet,contentDetails,statistics"
    }
    logger.info(f"Proxying YouTube video details for videoId: {video_id}, using API key")
    response = requests.get(youtube_api_url, params=params)
    if response.status_code != 200:
        logger.error("YouTube API error: " + response.text)
        abort(response.status_code, description="YouTube API error.")
    return jsonify(response.json())

def update_talent_videos():
    with app.app_context():
        logger.info("Running talent video update job...")
        try:
            talents = ApprovedTalent.query.all()
            logger.info(f"Found {len(talents)} approved talents")
            api_key = get_api_key()
            if not api_key:
                logger.error("Failed to retrieve API key for talent video update")
                return

            for talent in talents:
                youtube_api_url = "https://www.googleapis.com/youtube/v3/search"
                params = {
                    "key": api_key,
                    "channelId": talent.channel_id,  # Use channel_id directly, no @ removal
                    "part": "snippet",
                    "eventType": "live",
                    "type": "video",
                    "maxResults": 5
                }
                logger.info(f"Updating videos for {talent.talent_name} using API key")
                response = requests.get(youtube_api_url, params=params)
                if response.status_code == 200:
                    data = response.json()
                    for item in data.get('items', []):
                        video_id = item['id']['videoId']
                        if not TalentVideo.query.filter_by(video_id=video_id).first():
                            video = TalentVideo(
                                video_id=video_id,
                                title=item['snippet']['title'],
                                published_at=item['snippet']['publishedAt'],
                                talent_id=talent.id
                            )
                            db.session.add(video)
                    db.session.commit()
                    logger.info(f"Updated videos for {talent.talent_name}")
                else:
                    logger.error(f"YouTube API error for {talent.talent_name}: {response.text}")
        except Exception as e:
            logger.error(f"Error in update_talent_videos: {str(e)}")
            db.session.rollback()

@app.route('/dump_db', methods=['GET'])
def dump_db():
    try:
        if not os.path.exists(DB_PATH):
            logger.error(f"Database file not found at {DB_PATH}")
            return jsonify({"error": f"No database file at {DB_PATH}"}), 500

        logger.info(f"Database file found at {DB_PATH}")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]
        logger.info(f"Tables found: {tables}")

        if not tables:
            logger.warning("No tables in database")
            conn.close()
            return jsonify({"message": "Database exists but has no tables"}), 200

        result = {}
        for table in tables:
            cursor.execute(f"SELECT * FROM {table}")
            rows = cursor.fetchall()
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [col[1] for col in cursor.fetchall()]
            table_data = [dict(zip(columns, row)) for row in rows]
            result[table] = table_data
            logger.info(f"Dumped {len(table_data)} rows from {table}")

        conn.close()
        return jsonify({"database_path": DB_PATH, "tables": result})
    except Exception as e:
        logger.error(f"Error accessing database: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/talents', methods=['GET'])
def get_talents():
    try:
        logger.info(f"Before query - Database file exists: {os.path.exists(DB_PATH)}")
        talents = ApprovedTalent.query.all()
        result = [{"id": t.id, "talent_name": t.talent_name, "channel_id": t.channel_id} for t in talents]
        logger.info(f"Returning {len(result)} talents")
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error querying talents: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/talent_videos', methods=['GET'])
@limiter.limit("10 per minute")
def get_talent_videos():
    talents = ApprovedTalent.query.all()
    result = []
    for talent in talents:
        videos = TalentVideo.query.filter_by(talent_id=talent.id).all()
        video_list = [{"video_id": v.video_id, "published_at": v.published_at, "title": v.title} for v in videos]
        result.append({
            "talent_name": talent.talent_name,
            "channel_id": talent.channel_id,
            "videos": video_list
        })
    logger.info(f"Returning talent videos for {len(result)} talents")
    return jsonify(result)

@app.route('/watch', methods=['GET'])
@limiter.limit("20 per minute")
def watch_video():
    video_id = request.args.get('videoId')
    if not video_id:
        logger.warning("Missing videoId parameter in /watch request.")
        abort(400, description="Missing videoId parameter.")
    if not TalentVideo.query.filter_by(video_id=video_id).first():
        logger.warning(f"Unauthorized video_id attempted: {video_id}")
        abort(403, description="Forbidden: Video not approved.")
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Secure YouTube Player</title>
        <script src="https://www.youtube.com/iframe_api"></script>
        <script>
        var defaultVideoId = "{video_id}";
        var player;
        function onYouTubeIframeAPIReady() {{
            player = new YT.Player('player', {{
                height: '100%',
                width: '100%',
                videoId: defaultVideoId,
                playerVars: {{ 'autoplay': 1, 'controls': 0, 'modestbranding': 1, 'rel': 0 }},
                events: {{ 'onReady': onPlayerReady }}
            }});
        }}
        function onPlayerReady(event) {{ }}
        function loadVideo(videoId) {{
            player.loadVideoById(videoId);
            defaultVideoId = videoId;
        }}
        function fetchCommand() {{
            fetch('/get_next_video').then(response => {{
                if (!response.ok) throw new Error("Network response was not ok");
                return response.json();
            }}).then(data => {{
                if (data.videoId && data.videoId !== defaultVideoId) loadVideo(data.videoId);
            }}).catch(error => {{
                console.error('Error fetching video command:', error);
            }});
        }}
        setInterval(fetchCommand, 5000);
        </script>
    </head>
    <body style="margin:0;padding:0;background:black;">
        <div id="player"></div>
    </body>
    </html>
    """
    logger.info(f"Serving watch page for videoId: {video_id}")
    return html_content

# Scheduler Setup
try:
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=update_talent_videos, trigger="interval", minutes=60)
    scheduler.start()
    logger.info("Scheduler started successfully")
except Exception as e:
    logger.error(f"Failed to start scheduler: {str(e)}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
