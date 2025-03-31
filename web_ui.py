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
