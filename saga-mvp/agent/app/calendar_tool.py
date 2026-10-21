"""Small Google Calendar adapter used by the agent and meeting UI."""
from functools import lru_cache

from google.oauth2 import service_account
from googleapiclient.discovery import build

from . import config

SCOPES = ["https://www.googleapis.com/auth/calendar"]


@lru_cache(maxsize=1)
def _service():
    creds = service_account.Credentials.from_service_account_file(
        config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def create_event(summary: str, start_iso: str, end_iso: str, attendees: list[str] | None = None) -> dict:
    body = {"summary": summary, "start": {"dateTime": start_iso}, "end": {"dateTime": end_iso}}
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    return _service().events().insert(calendarId=config.GOOGLE_CALENDAR_ID, body=body).execute()


def list_events(time_min_iso: str, time_max_iso: str, max_results: int = 100) -> list[dict]:
    result = (
        _service().events().list(
            calendarId=config.GOOGLE_CALENDAR_ID,
            timeMin=time_min_iso,
            timeMax=time_max_iso,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
    )
    return result.get("items", [])


def list_upcoming_events(max_results: int = 10) -> list[dict]:
    import datetime
    now = datetime.datetime.utcnow().isoformat() + "Z"
    later = (datetime.datetime.utcnow() + datetime.timedelta(days=30)).isoformat() + "Z"
    return list_events(now, later, max_results=max_results)
