# -*- coding: utf-8 -*-
import os
import logging
import requests
import sqlite3  # Added for dump_db endpoint
import json
import struct  # Added for binary decoding
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from google.cloud import secretmanager
from google.oauth2 import service_account
from xml.etree import ElementTree as ET

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

# Subscription management
def subscribe_to_channel(channel_id):
    hub_url = "https://pubsubhubbub.appspot.com/subscribe"
    callback_url = "https://skyward-production.up.railway.app/youtube/webhook"
    data = {
        "hub.mode": "subscribe",
        "hub.topic": f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
        "hub.callback": callback_url
    }
    logger.info(f"Attempting subscription to {channel_id}")
    response = requests.post(hub_url, data=data)
    if response.status_code in (202, 204):
        logger.info(f"Subscription queued for {channel_id} - Status: {response.status_code}")
    else:
        logger.error(f"Subscription failed for {channel_id} - Status: {response.status_code}, Response: {response.text}")
    return response.status_code in (202, 204)

def unsubscribe_from_channel(channel_id):
    hub_url = "https://pubsubhubbub.appspot.com/subscribe"
    callback_url = "https://skyward-production.up.railway.app/youtube/webhook"
    data = {
        "hub.mode": "unsubscribe",
        "hub.topic": f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
        "hub.callback": callback_url
    }
    response = requests.post(hub_url, data=data)
    if response.status_code in (202, 204):
        logger.info(f"Unsubscribed from channel: {channel_id}")
    else:
        logger.error(f"Failed to unsubscribe from {channel_id}: {response.text}")

def check_and_cache_live_videos():
    with app.app_context():
        session = db.session
        talents = session.query(ApprovedTalent).all()
        logger.info(f"Checking activities for {len(talents)} talents")
        api_key = get_api_key()

        for talent in talents:
            response = requests.get(
                "https://www.googleapis.com/youtube/v3/activities",
                params={
                    "key": api_key,
                    "channelId": talent.channel_id,
                    "part": "snippet,contentDetails",
                    "maxResults": 10
                }
            )
            if response.status_code != 200:
                logger.error(f"Failed activities.list for {talent.channel_id}: {response.text}")
                continue

            data = response.json()
            uploads = [item for item in data.get('items', []) if item['snippet']['type'] == 'upload']
            if not uploads:
                continue

            for upload in uploads:
                video_id = upload['contentDetails']['upload']['videoId']
                video_response = requests.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={"key": api_key, "id": video_id, "part": "liveStreamingDetails,snippet"}
                )
                if video_response.status_code == 200:
                    video_data = video_response.json()
                    if (video_data['items'] and 
                        'liveStreamingDetails' in video_data['items'][0] and 
                        'actualEndTime' not in video_data['items'][0]['liveStreamingDetails']):
                        if not session.query(TalentVideo).filter_by(video_id=video_id).first():
                            video = TalentVideo(
                                video_id=video_id,
                                title=video_data['items'][0]['snippet']['title'],
                                published_at=video_data['items'][0]['snippet']['publishedAt'],
                                talent_id=talent.id
                            )
                            session.add(video)
                            logger.info(f"Cached live video: {video_id} for {talent.talent_name}")
            session.commit()

def renew_subscriptions():
    with app.app_context():
        session = db.session
        talents = session.query(ApprovedTalent).all()
        logger.info(f"Renewing subscriptions for {len(talents)} talents")
        for talent in talents:
            subscribe_to_channel(talent.channel_id)
        session.commit()

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

# Webhook for PubSubHubbub notifications
@app.route('/youtube/webhook', methods=['GET', 'POST'])
def youtube_webhook():
    if request.method == 'GET':
        challenge = request.args.get('hub.challenge')
        mode = request.args.get('hub.mode', 'unknown')
        topic = request.args.get('hub.topic', 'unknown')
        logger.info(f"Webhook verification - Mode: {mode}, Topic: {topic}, Challenge: {challenge}")
        return challenge, 200

    xml_data = request.data.decode('utf-8')
    logger.info(f"Received webhook notification: {xml_data[:100]}...")
    root = ET.fromstring(xml_data)
    video_id = root.find('.//{http://www.youtube.com/xml/schemas/2015}videoId').text
    channel_id = root.find('.//{http://www.youtube.com/xml/schemas/2015}channelId').text
    published_at = root.find('.//{http://www.w3.org/2005/Atom}published').text

    api_key = get_api_key()
    response = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"key": api_key, "id": video_id, "part": "liveStreamingDetails,snippet"}
    )
    if response.status_code == 200:
        data = response.json()
        if data['items'] and 'liveStreamingDetails' in data['items'][0]:
            session = db.session
            talent = session.query(ApprovedTalent).filter_by(channel_id=channel_id).first()
            if talent and not session.query(TalentVideo).filter_by(video_id=video_id).first():
                video = TalentVideo(
                    video_id=video_id,
                    title=data['items'][0]['snippet']['title'],
                    published_at=published_at,
                    talent_id=talent.id
                )
                session.add(video)
                session.commit()
                logger.info(f"Cached live video: {video_id} for {talent.talent_name}")
    return '', 204

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
    api_key = get_api_key()
    if not api_key:
        logger.error("API key retrieval failed for /set_video")
        abort(500, description="Internal server error: API key unavailable")
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
        "part": "snippet,contentDetails,statistics,liveStreamingDetails"
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

@app.route('/refresh_videos', methods=['GET'])
def refresh_videos():
    check_and_cache_live_videos()
    return jsonify({"status": "Videos refreshed"}), 200

# Test routes
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

@app.route('/test_subscription')
def test_subscription():
    with app.app_context():
        subscribe_to_channel("UCgnfPPb9JI3e9A4cXHnWbyg")
    return "Subscription test triggered", 200

@app.route('/renew_subscriptions')
def trigger_renew_subscriptions():
    with app.app_context():
        check_and_cache_live_videos()  # Check activities first
        renew_subscriptions()
    return "Subscriptions renewed and activities checked", 200

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

# Display
@app.route('/watch', methods=['GET'])
@limiter.limit("20 per minute")
def watch_video():
    video_id = request.args.get('videoId')
    if not video_id or not TalentVideo.query.filter_by(video_id=video_id).first():
        abort(403, description="Forbidden: Video not approved.")
    html = f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:black;">
        <iframe id="player" width="100%" height="100%" src="https://www.youtube.com/embed/{video_id}?autoplay=1&controls=1&modestbranding=1&rel=0" frameborder="0" allowfullscreen></iframe>
    </body>
    </html>
    """
    logger.info(f"Serving watch page for videoId: {video_id}")
    return html

# Scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(renew_subscriptions, 'interval', days=6)
scheduler.start()

if __name__ == '__main__':
    setup_credentials()
    with app.app_context():
        check_and_cache_live_videos()  # Initial check on startup
        renew_subscriptions()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
