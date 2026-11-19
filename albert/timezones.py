"""Time zone list for the onboarding form, built from the IANA tables in the tzdata package."""

from functools import cache
from importlib.resources import files

# IANA names a zone after one city, so people searching for their state or a
# bigger city find nothing. Extra search words per zone, kept short on purpose.
ALIASES = {
    "UTC": "GMT Coordinated Universal Time Zulu",
    # United States
    "America/New_York": "New York City NYC Boston Massachusetts Washington DC Philadelphia Pennsylvania "
                        "Atlanta Georgia Miami Florida New Jersey Virginia North Carolina South Carolina "
                        "Ohio Maryland Connecticut Maine Vermont New Hampshire Rhode Island Delaware West Virginia EST EDT",
    "America/Chicago": "Texas Houston Dallas Austin San Antonio Illinois Minnesota Minneapolis Wisconsin "
                       "Missouri St. Louis Iowa Louisiana New Orleans Alabama Mississippi Oklahoma Arkansas "
                       "Kansas Nebraska Tennessee Nashville CST CDT",
    "America/Denver": "Colorado Utah Salt Lake City New Mexico Albuquerque Wyoming Montana MST MDT",
    "America/Phoenix": "Arizona",
    "America/Los_Angeles": "California San Francisco Bay Area Silicon Valley San Diego San Jose Sacramento "
                           "Seattle Washington State Portland Oregon Nevada Las Vegas PST PDT",
    "America/Anchorage": "Alaska",
    "Pacific/Honolulu": "Hawaii",
    "America/Detroit": "Michigan",
    "America/Indiana/Indianapolis": "Indiana",
    "America/Kentucky/Louisville": "Kentucky",
    "America/Boise": "Idaho",
    # Canada
    "America/Toronto": "Ontario Ottawa Quebec Montreal",
    "America/Vancouver": "British Columbia",
    "America/Edmonton": "Alberta Calgary",
    "America/Winnipeg": "Manitoba",
    "America/Regina": "Saskatchewan",
    "America/Halifax": "Nova Scotia",
    "America/St_Johns": "Newfoundland",
    # Australia
    "Australia/Sydney": "New South Wales Canberra",
    "Australia/Melbourne": "Victoria",
    "Australia/Brisbane": "Queensland",
    "Australia/Perth": "Western Australia",
    "Australia/Adelaide": "South Australia",
    # Europe
    "Europe/Zurich": "Geneva Basel Bern Lausanne Lugano Lucerne Winterthur",
    "Europe/Berlin": "Munich Hamburg Frankfurt Cologne Stuttgart Dusseldorf",
    "Europe/London": "England Scotland Wales Manchester Birmingham Edinburgh Glasgow UK",
    "Europe/Paris": "Lyon Marseille Nice Toulouse",
    "Europe/Rome": "Milan Naples Turin Florence",
    "Europe/Madrid": "Barcelona Valencia Seville",
    "Europe/Amsterdam": "Rotterdam The Hague Holland",
    "Europe/Vienna": "Salzburg Graz Innsbruck",
    "Europe/Kyiv": "Kiev",
    # Asia / Americas
    "Asia/Kolkata": "Mumbai Delhi Bangalore Bengaluru Chennai Hyderabad Calcutta IST",
    "Asia/Shanghai": "Beijing Shenzhen Guangzhou",
    "Asia/Tokyo": "Osaka Kyoto JST",
    "Asia/Dubai": "Abu Dhabi",
    "Asia/Ho_Chi_Minh": "Saigon Hanoi",
    "Asia/Kathmandu": "Katmandu",
    "America/Sao_Paulo": "Rio de Janeiro Brasilia",
}


def _read_tab(name: str) -> list[list[str]]:
    text = files("tzdata.zoneinfo").joinpath(name).read_text(encoding="utf-8")
    return [line.split("\t") for line in text.splitlines() if line and not line.startswith("#")]


@cache
def list_timezones() -> list[dict]:
    """Return [{id, country, keywords}] for every zone in zone.tab, plus UTC."""
    countries = {row[0]: row[1] for row in _read_tab("iso3166.tab")}
    zones = [{"id": "UTC", "country": "", "keywords": ALIASES["UTC"]}]
    for row in _read_tab("zone.tab"):
        zone_id = row[2]
        comment = row[3] if len(row) > 3 else ""
        zones.append({
            "id": zone_id,
            "country": countries.get(row[0], row[0]),
            "keywords": " ".join(filter(None, [comment, ALIASES.get(zone_id, "")])),
        })
    return zones
