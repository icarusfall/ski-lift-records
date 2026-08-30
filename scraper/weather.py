"""Fetch daily weather data from Open-Meteo API."""
import requests

API_URL = "https://api.open-meteo.com/v1/forecast"

DAILY_FIELDS = [
    "wind_gusts_10m_max",
    "wind_speed_10m_max",
    "temperature_2m_min",
    "temperature_2m_max",
    "snowfall_sum",
    "precipitation_sum",
    "weather_code",
    "sunshine_duration",
]

# Hourly fields summarised to daily extremes. 700hPa sits near ~3000m, so its
# wind is a far better proxy for summit-lift conditions than a 10m surface
# reading, and the freezing level says whether precipitation fell as snow.
HOURLY_FIELDS = [
    "freezing_level_height",
    "wind_speed_700hPa",
]


def fetch_weather(latitude: float, longitude: float, elevation: int | None = None) -> dict:
    """Return yesterday's weather summary for given coordinates."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": ",".join(DAILY_FIELDS),
        "hourly": ",".join(HOURLY_FIELDS),
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
        hourly = data.get("hourly", {})

        sunshine_s = _first(daily, "sunshine_duration")
        freezing = _values(hourly, "freezing_level_height")
        wind700 = _values(hourly, "wind_speed_700hPa")

        return {
            "wind_gust_max_kmh":  _first(daily, "wind_gusts_10m_max"),
            "wind_speed_max_kmh": _first(daily, "wind_speed_10m_max"),
            "temp_min_c":         _first(daily, "temperature_2m_min"),
            "temp_max_c":         _first(daily, "temperature_2m_max"),
            "fresh_snow_cm":      _first(daily, "snowfall_sum"),
            "precipitation_mm":   _first(daily, "precipitation_sum"),
            "weather_code":       _first(daily, "weather_code"),
            "sunshine_hours":     round(sunshine_s / 3600, 1) if sunshine_s is not None else None,
            "freezing_level_max_m": round(max(freezing)) if freezing else None,
            "freezing_level_min_m": round(min(freezing)) if freezing else None,
            "wind_700hpa_max_kmh":  round(max(wind700), 1) if wind700 else None,
        }
    except Exception:
        return {}


def _first(daily: dict, key: str):
    """Return the first value from a daily array, or None."""
    values = daily.get(key, [])
    return values[0] if values else None


def _values(hourly: dict, key: str) -> list[float]:
    """Non-null values from an hourly array."""
    return [v for v in hourly.get(key, []) if v is not None]
