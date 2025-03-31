from fastapi import FastAPI, Form, Depends, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import requests
import tweepy  # Twitter API
import facebook  # Facebook API (requires facebook-sdk)
from atproto import Client  # Bluesky API
import googleapiclient.discovery  # YouTube API
import time
import threading

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Placeholder for user settings
user_settings = {
    "bluesky_api_key": "",
    "facebook_api_key": "",
    "twitter_api_key": "",
    "youtube_api_key": "",
    "youtube_channel_id": "",
    "nws_location": ""
}

latest_youtube_video = None

def get_weather_alerts(location: str):
    """Fetch weather alerts from the NWS API"""
    url = f"https://api.weather.gov/alerts/active?area={location}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    return {}

def post_to_twitter(message: str):
    """Post message to Twitter/X"""
    auth = tweepy.OAuthHandler(user_settings["twitter_api_key"], "API_SECRET")
    auth.set_access_token("ACCESS_TOKEN", "ACCESS_SECRET")
    api = tweepy.API(auth)
    api.update_status(message)

def post_to_facebook(message: str):
    """Post message to Facebook"""
    graph = facebook.GraphAPI(access_token=user_settings["facebook_api_key"])
    graph.put_object(parent_object='me', connection_name='feed', message=message)

def post_to_bluesky(message: str):
    """Post message to Bluesky"""
    client = Client()
    client.login(user_settings["bluesky_api_key"], "PASSWORD")
    client.post_message(message)

def check_youtube_updates():
    """Continuously check for new YouTube videos."""
    global latest_youtube_video
    youtube = googleapiclient.discovery.build("youtube", "v3", developerKey=user_settings["youtube_api_key"])
    while True:
        request = youtube.search().list(
            part="snippet",
            channelId=user_settings["youtube_channel_id"],
            order="date",
            maxResults=1
        )
        response = request.execute()
        if "items" in response and response["items"]:
            video_id = response["items"][0]["id"].get("videoId")
            if video_id and video_id != latest_youtube_video:
                latest_youtube_video = video_id
                video_url = f"https://www.youtube.com/watch?v={video_id}"
                message = f"New YouTube video posted: {video_url}"
                post_to_twitter(message)
                post_to_facebook(message)
                post_to_bluesky(message)
        time.sleep(300)  # Check every 5 minutes

@app.on_event("startup")
def start_youtube_monitor():
    """Start a background thread to monitor YouTube uploads."""
    thread = threading.Thread(target=check_youtube_updates, daemon=True)
    thread.start()

@app.post("/update-settings/")
def update_settings(bluesky_api_key: str = Form(...), facebook_api_key: str = Form(...), twitter_api_key: str = Form(...), youtube_api_key: str = Form(...), youtube_channel_id: str = Form(...), nws_location: str = Form(...)):
    """Update user-defined API keys and location settings"""
    user_settings.update({
        "bluesky_api_key": bluesky_api_key,
        "facebook_api_key": facebook_api_key,
        "twitter_api_key": twitter_api_key,
        "youtube_api_key": youtube_api_key,
        "youtube_channel_id": youtube_channel_id,
        "nws_location": nws_location
    })
    return {"message": "Settings updated successfully"}

@app.post("/post-to-social/")
def post_to_social(data: PostData):
    """Post a message to selected social media platforms"""
    if data.post_to_bluesky:
        post_to_bluesky(data.message)
    if data.post_to_facebook:
        post_to_facebook(data.message)
    if data.post_to_twitter:
        post_to_twitter(data.message)
    return {"message": "Post sent to selected platforms"}

@app.get("/weather-alerts/")
def fetch_weather_alerts():
    """Retrieve weather alerts based on user-defined location"""
    location = user_settings.get("nws_location", "")
    if not location:
        return {"error": "Location not set"}
    alerts = get_weather_alerts(location)
    return alerts

@app.get("/")
def home(request: Request):
    """Render the main web interface"""
    return templates.TemplateResponse("index.html", {"request": request, "settings": user_settings})
