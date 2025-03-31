def get_weather_alerts(location: str):
    """Fetch weather alerts from the NWS API"""
    url = f"https://api.weather.gov/alerts/active?area={location}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    return {}
