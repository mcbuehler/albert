"""Brave web search for the assistant's search tool."""

import requests
from flask import current_app

from albert import config


def search_brave(query: str, max_results: int = 10) -> tuple[list, str]:
    """Return the top Brave web search results and a status message."""
    query = query.strip()
    if not query:
        return [], "Search query cannot be empty."
    if len(query) > 600:
        return [], "Search query cannot exceed 600 characters."
    if len(query.split()) > 75:
        return [], "Search query cannot exceed 75 words."
    if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 20:
        return [], "Search result count must be an integer between 1 and 20."

    api_key = config.env("BRAVE_API_KEY")
    if not api_key:
        return [], "Search API error: BRAVE_API_KEY is not configured."

    try:
        response = requests.get(
            config.BRAVE_SEARCH_URL,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
            params={"q": query, "count": max_results, "text_decorations": False, "freshness": "pd", "maximum_number_of_tokens": 2048},
            timeout=4,
        )

        if response.status_code != 200:
            message = f"Search API error: {response.status_code}"
            try:
                body = response.json()
                error = body.get("error") if isinstance(body, dict) else None
                detail = (error.get("detail") or error.get("code")) if isinstance(error, dict) else None
                if isinstance(detail, (str, int)) and not isinstance(detail, bool) and detail:
                    message += f" - {detail}"
            except ValueError as e:
                current_app.logger.error(
                    "Brave search error response with status %s was not valid JSON (%s)",
                    response.status_code, type(e).__name__,
                )
            return [], message

        try:
            body = response.json()
        except ValueError as e:
            current_app.logger.error("Brave search success response was not valid JSON (%s)", type(e).__name__)
            return [], "Failed to perform web search."
        if not isinstance(body, dict):
            raise ValueError("Brave search response must be an object.")
        grounding = body.get("grounding", {})
        if not isinstance(grounding, dict):
            raise ValueError("Brave search response field 'grounding' must be an object.")
        generic_results = grounding.get("generic", [])
        if not isinstance(generic_results, list):
            raise ValueError("Brave search response field 'grounding.generic' must be an array.")
        if not generic_results:
            return [], "No relevant search results found."
        results = []
        for index, item in enumerate(generic_results):
            if not isinstance(item, dict) or not isinstance(item.get("title"), str):
                raise ValueError(f"Brave search result {index} must be an object with string 'title'.")
            snippets = item.get("snippets")
            if not isinstance(snippets, list) or not all(isinstance(snippet, str) for snippet in snippets):
                raise ValueError(f"Brave search result {index} field 'snippets' must be an array of strings.")
            results.append({item["title"]: " ".join(snippets)})
        return results, "Success"
    except (requests.RequestException, ValueError):
        current_app.logger.exception("Brave search request or response validation failed")
        return [], "Failed to perform web search."
