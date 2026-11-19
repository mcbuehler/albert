"""Weather lookup (wttr.in) for the assistant's weather tool."""

import urllib.parse

import requests

from albert.logging_utils import get_logger


def get_weather(city: str, language: str = 'en') -> str:
    """Return a concise, LLM-friendly weather line for a city."""
    encoded_city = urllib.parse.quote(city.strip())
    # Encode the language too, so a value like "de&format=j1" can't add query parameters.
    encoded_language = urllib.parse.quote(language.strip(), safe="")

    # Query wttr.in for a concise, LLM-friendly string
    # ?m uses the metric system. Use ?u for imperial.
    wttr_url = f"https://wttr.in/{encoded_city}?m&format=Condition:+%C,+Temp:+%t,+Wind:+%w&lang={encoded_language}"

    unavailable = f"Unable to retrieve weather for {city} right now."
    try:
        response = requests.get(wttr_url, timeout=5)
    except requests.RequestException as e:
        get_logger(__name__).error("Weather request failed (%s)", type(e).__name__)
        return unavailable
    except Exception:
        get_logger(__name__).exception("Weather request failed unexpectedly")
        return unavailable
    # wttr.in error pages (e.g. 503 "Unknown location" or overload) must not be read out as weather.
    if not response.ok:
        get_logger(__name__).error("Weather service returned HTTP %s", response.status_code)
        return unavailable
    if not isinstance(response.text, str):
        get_logger(__name__).error("Weather service returned a non-text success response")
        return unavailable
    return response.text.strip()
