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
