"""Fetch daily weather data from Open-Meteo API."""
import requests

API_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_weather(latitude: float, longitude: float, elevation: int | None = None) -> dict:
    """Return yesterday's weather summary for given coordinates."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": ",".join([
            "wind_gusts_10m_max",
            "wind_speed_10m_max",
            "temperature_2m_min",
            "temperature_2m_max",
            "snowfall_sum",
            "precipitation_sum",
            "weather_code",
        ]),
        "timezone": "UTC",
        "past_days": 1,
        "forecast_days": 0,
    }
    if elevation is not None:
        params["elevation"] = elevation
    try:
        resp = requests.get(API_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        return {
            "wind_gust_max_kmh":  _first(daily, "wind_gusts_10m_max"),
            "wind_speed_max_kmh": _first(daily, "wind_speed_10m_max"),
            "temp_min_c":         _first(daily, "temperature_2m_min"),
            "temp_max_c":         _first(daily, "temperature_2m_max"),
            "fresh_snow_cm":      _first(daily, "snowfall_sum"),
            "precipitation_mm":   _first(daily, "precipitation_sum"),
            "weather_code":       _first(daily, "weather_code"),
        }
    except Exception:
        return {}


def _first(daily: dict, key: str):
    """Return the first value from a daily array, or None."""
    values = daily.get(key, [])
    return values[0] if values else None
