"""
Google Calendar MCP Tools

This module provides MCP tools for interacting with Google Calendar API.
"""

import datetime
import logging
import asyncio
import re
import uuid
import json
from typing import List, Optional, Dict, Any, Union
from auth.authentik_decorator import require_authentik
from google.calendar_service import get_calendar_service

from googleapiclient.errors import HttpError
from googleapiclient.discovery import build


from core.utils import handle_http_errors

from core.server import server


# Configure module logger
logger = logging.getLogger(__name__)


def _parse_reminders_json(reminders_input: Optional[Union[str, List[Dict[str, Any]]]], function_name: str) -> List[Dict[str, Any]]:
    """
    Parse reminders from JSON string or list object and validate them.
    
    Args:
        reminders_input: JSON string containing reminder objects or list of reminder objects
        function_name: Name of calling function for logging
        
    Returns:
        List of validated reminder objects
    """
    if not reminders_input:
        return []
    
    # Handle both string (JSON) and list inputs
    if isinstance(reminders_input, str):
        try:
            reminders = json.loads(reminders_input)
            if not isinstance(reminders, list):
                logger.warning(f"[{function_name}] Reminders must be a JSON array, got {type(reminders).__name__}")
                return []
        except json.JSONDecodeError as e:
            logger.warning(f"[{function_name}] Invalid JSON for reminders: {e}")
            return []
    elif isinstance(reminders_input, list):
        reminders = reminders_input
    else:
        logger.warning(f"[{function_name}] Reminders must be a JSON string or list, got {type(reminders_input).__name__}")
        return []
    
    # Validate reminders
    if len(reminders) > 5:
        logger.warning(f"[{function_name}] More than 5 reminders provided, truncating to first 5")
        reminders = reminders[:5]
    
    validated_reminders = []
    for reminder in reminders:
        if not isinstance(reminder, dict) or "method" not in reminder or "minutes" not in reminder:
            logger.warning(f"[{function_name}] Invalid reminder format: {reminder}, skipping")
            continue
        
        method = reminder["method"].lower()
        if method not in ["popup", "email"]:
            logger.warning(f"[{function_name}] Invalid reminder method '{method}', must be 'popup' or 'email', skipping")
            continue
        
        minutes = reminder["minutes"]
        if not isinstance(minutes, int) or minutes < 0 or minutes > 40320:
            logger.warning(f"[{function_name}] Invalid reminder minutes '{minutes}', must be integer 0-40320, skipping")
            continue
        
        validated_reminders.append({
            "method": method,
            "minutes": minutes
        })
    
    return validated_reminders


def _apply_transparency_if_valid(
    event_body: Dict[str, Any],
    transparency: Optional[str],
    function_name: str,
) -> None:
    """
    Apply transparency to the event body if the provided value is valid.

    Args:
        event_body: Event payload being constructed.
        transparency: Provided transparency value.
        function_name: Name of the calling function for logging context.
    """
    if transparency is None:
        return

    valid_transparency_values = ["opaque", "transparent"]
    if transparency in valid_transparency_values:
        event_body["transparency"] = transparency
        logger.info(f"[{function_name}] Set transparency to '{transparency}'")
    else:
        logger.warning(
            f"[{function_name}] Invalid transparency value '{transparency}', must be 'opaque' or 'transparent', skipping"
        )


def _preserve_existing_fields(event_body: Dict[str, Any], existing_event: Dict[str, Any], field_mappings: Dict[str, Any]) -> None:
    """
    Helper function to preserve existing event fields when not explicitly provided.

    Args:
        event_body: The event body being built for the API call
        existing_event: The existing event data from the API
        field_mappings: Dict mapping field names to their new values (None means preserve existing)
    """
    for field_name, new_value in field_mappings.items():
        if new_value is None and field_name in existing_event:
            event_body[field_name] = existing_event[field_name]
            logger.info(f"[modify_event] Preserving existing {field_name}")
        elif new_value is not None:
            event_body[field_name] = new_value


def _format_attendee_details(attendees: List[Dict[str, Any]], indent: str = "  ") -> str:
    """
    Format attendee details including response status, organizer, and optional flags.

    Example output format:
    "  user@example.com: accepted
  manager@example.com: declined (organizer)
  optional-person@example.com: tentative (optional)"

    Args:
        attendees: List of attendee dictionaries from Google Calendar API
        indent: Indentation to use for newline-separated attendees (default: "  ")

    Returns:
        Formatted string with attendee details, or "None" if no attendees
    """
    if not attendees:
        return "None"

    attendee_details_list = []
    for a in attendees:
        email = a.get("email", "unknown")
        response_status = a.get("responseStatus", "unknown")
        optional = a.get("optional", False)
        organizer = a.get("organizer", False)

        detail_parts = [f"{email}: {response_status}"]
        if organizer:
            detail_parts.append("(organizer)")
        if optional:
            detail_parts.append("(optional)")

        attendee_details_list.append(" ".join(detail_parts))

    return f"\n{indent}".join(attendee_details_list)


def _format_attachment_details(attachments: List[Dict[str, Any]], indent: str = "  ") -> str:
    """
    Format attachment details including file information.


    Args:
        attachments: List of attachment dictionaries from Google Calendar API
        indent: Indentation to use for newline-separated attachments (default: "  ")

    Returns:
        Formatted string with attachment details, or "None" if no attachments
    """
    if not attachments:
        return "None"

    attachment_details_list = []
    for att in attachments:
        title = att.get("title", "Untitled")
        file_url = att.get("fileUrl", "No URL")
        file_id = att.get("fileId", "No ID")
        mime_type = att.get("mimeType", "Unknown")

        attachment_info = (
            f"{title}\n"
            f"{indent}File URL: {file_url}\n"
            f"{indent}File ID: {file_id}\n"
            f"{indent}MIME Type: {mime_type}"
        )
        attachment_details_list.append(attachment_info)

    return f"\n{indent}".join(attachment_details_list)


# Helper function to ensure time strings for API calls are correctly formatted
def _correct_time_format_for_api(
    time_str: Optional[str], param_name: str
) -> Optional[str]:
    if not time_str:
        return None

    logger.info(
        f"_correct_time_format_for_api: Processing {param_name} with value '{time_str}'"
    )

    # Handle date-only format (YYYY-MM-DD)
    if len(time_str) == 10 and time_str.count("-") == 2:
        try:
            # Validate it's a proper date
            datetime.datetime.strptime(time_str, "%Y-%m-%d")
            # For date-only, append T00:00:00Z to make it RFC3339 compliant
            formatted = f"{time_str}T00:00:00Z"
            logger.info(
                f"Formatting date-only {param_name} '{time_str}' to RFC3339: '{formatted}'"
            )
            return formatted
        except ValueError:
            logger.warning(
                f"{param_name} '{time_str}' looks like a date but is not valid YYYY-MM-DD. Using as is."
            )
            return time_str

    # Specifically address YYYY-MM-DDTHH:MM:SS by appending 'Z'
    if (
        len(time_str) == 19
        and time_str[10] == "T"
        and time_str.count(":") == 2
        and not (
            time_str.endswith("Z") or ("+" in time_str[10:]) or ("-" in time_str[10:])
        )
    ):
        try:
            # Validate the format before appending 'Z'
            datetime.datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S")
            logger.info(
                f"Formatting {param_name} '{time_str}' by appending 'Z' for UTC."
            )
            return time_str + "Z"
        except ValueError:
            logger.warning(
                f"{param_name} '{time_str}' looks like it needs 'Z' but is not valid YYYY-MM-DDTHH:MM:SS. Using as is."
            )
            return time_str

    # If it already has timezone info or doesn't match our patterns, return as is
    logger.info(f"{param_name} '{time_str}' doesn't need formatting, using as is.")
    return time_str
@server.tool()
@handle_http_errors("list_calendars", is_read_only=True, service_type="calendar")
@require_authentik()
async def list_calendars(request, user) -> str:
    """
    Retrieves a list of calendars accessible to the authenticated user
    using Authentik for identity and Google Workspace delegation.
    """

    # 🔐 Identity from Authentik
    user_google_email = user.email

    # 🔑 Google Calendar service via service account delegation
    service = get_calendar_service(user_google_email)

    logger.info(f"[list_calendars] Invoked. Email: '{user_google_email}'")

    calendar_list_response = await asyncio.to_thread(
        lambda: service.calendarList().list().execute()
    )

    items = calendar_list_response.get("items", [])
    if not items:
        return f"No calendars found for {user_google_email}."

    calendars_summary_list = [
        f'- "{cal.get("summary", "No Summary")}"'
        f'{" (Primary)" if cal.get("primary") else ""} '
        f'(ID: {cal["id"]})'
        for cal in items
    ]

    result = (
        f"Successfully listed {len(items)} calendars for {user_google_email}:\n"
        + "\n".join(calendars_summary_list)
    )

    logger.info(f"Successfully listed {len(items)} calendars for {user_google_email}.")
    return result



@server.tool()
@handle_http_errors("get_events", is_read_only=True, service_type="calendar")
@require_authentik()
async def get_events(
    request,
    user,
    calendar_id: str = "primary",
    event_id: Optional[str] = None,
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    max_results: int = 25,
    query: Optional[str] = None,
    detailed: bool = False,
    include_attachments: bool = False,
) -> str:
    """
    Retrieves events from a specified Google Calendar using Authentik for auth
    and Google Workspace Domain-Wide Delegation for API access.
    """

    # 🔐 Identity from Authentik
    user_google_email = user.email

    # 🔑 Google Calendar service via delegation
    service = get_calendar_service(user_google_email)

    logger.info(
        f"[get_events] User: '{user_google_email}', event_id: '{event_id}', "
        f"time_min: '{time_min}', time_max: '{time_max}', query: '{query}', "
        f"detailed: {detailed}, include_attachments: {include_attachments}"
    )

    # --- Single event ---
    if event_id:
        logger.info(f"[get_events] Retrieving single event ID: {event_id}")
        event = await asyncio.to_thread(
            lambda: service.events()
            .get(calendarId=calendar_id, eventId=event_id)
            .execute()
        )
        items = [event]

    # --- Multiple events ---
    else:
        formatted_time_min = _correct_time_format_for_api(time_min, "time_min")
        if formatted_time_min:
            effective_time_min = formatted_time_min
        else:
            utc_now = datetime.datetime.now(datetime.timezone.utc)
            effective_time_min = utc_now.isoformat().replace("+00:00", "Z")

        effective_time_max = _correct_time_format_for_api(time_max, "time_max")

        request_params = {
            "calendarId": calendar_id,
            "timeMin": effective_time_min,
            "timeMax": effective_time_max,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }

        if query:
            request_params["q"] = query

        events_result = await asyncio.to_thread(
            lambda: service.events().list(**request_params).execute()
        )
        items = events_result.get("items", [])

    # --- No events ---
    if not items:
        if event_id:
            return (
                f"Event with ID '{event_id}' not found in calendar "
                f"'{calendar_id}' for {user_google_email}."
            )
        return (
            f"No events found in calendar '{calendar_id}' "
            f"for {user_google_email}."
        )

    # --- Detailed single event ---
    if event_id and detailed:
        item = items[0]
        summary = item.get("summary", "No Title")
        start = item["start"].get("dateTime", item["start"].get("date"))
        end = item["end"].get("dateTime", item["end"].get("date"))
        link = item.get("htmlLink", "No Link")
        description = item.get("description", "No Description")
        location = item.get("location", "No Location")
        attendees = item.get("attendees", [])

        attendee_details_str = _format_attendee_details(attendees, indent="  ")

        event_details = (
            f"Event Details:\n"
            f"- Title: {summary}\n"
            f"- Starts: {start}\n"
            f"- Ends: {end}\n"
            f"- Description: {description}\n"
            f"- Location: {location}\n"
            f"- Attendee Details: {attendee_details_str}\n"
        )

        if include_attachments:
            attachments = item.get("attachments", [])
            attachment_details_str = _format_attachment_details(
                attachments, indent="  "
            )
            event_details += f"- Attachments: {attachment_details_str}\n"

        event_details += f"- Event ID: {event_id}\n- Link: {link}"
        return event_details

    # --- Multiple events output ---
    event_details_list = []
    for item in items:
        summary = item.get("summary", "No Title")
        start_time = item["start"].get("dateTime", item["start"].get("date"))
        end_time = item["end"].get("dateTime", item["end"].get("date"))
        link = item.get("htmlLink", "No Link")
        item_event_id = item.get("id", "No ID")

        if detailed:
            description = item.get("description", "No Description")
            location = item.get("location", "No Location")
            attendees = item.get("attendees", [])
            attendee_details_str = _format_attendee_details(
                attendees, indent="    "
            )

            detail = (
                f'- "{summary}" (Starts: {start_time}, Ends: {end_time})\n'
                f"  Description: {description}\n"
                f"  Location: {location}\n"
                f"  Attendee Details: {attendee_details_str}\n"
            )

            if include_attachments:
                attachments = item.get("attachments", [])
                detail += (
                    f"  Attachments: "
                    f"{_format_attachment_details(attachments, indent='    ')}\n"
                )

            detail += f"  ID: {item_event_id} | Link: {link}"
            event_details_list.append(detail)

        else:
            event_details_list.append(
                f'- "{summary}" (Starts: {start_time}, Ends: {end_time}) '
                f"ID: {item_event_id} | Link: {link}"
            )

    return (
        f"Successfully retrieved {len(items)} events from calendar "
        f"'{calendar_id}' for {user_google_email}:\n"
        + "\n".join(event_details_list)
    )



@server.tool()
@handle_http_errors("create_event", service_type="calendar")
@require_authentik()
async def create_event(
    request,
    user,
    summary: str,
    start_time: str,
    end_time: str,
    calendar_id: str = "primary",
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    timezone: Optional[str] = None,
    attachments: Optional[List[str]] = None,
    add_google_meet: bool = False,
    reminders: Optional[Union[str, List[Dict[str, Any]]]] = None,
    use_default_reminders: bool = True,
    transparency: Optional[str] = None,
) -> str:
    """
    Creates a new Google Calendar event using Authentik for identity
    and Google Workspace domain-wide delegation for API access.
    """

    # 🔐 Identity from Authentik
    user_google_email = user.email

    # 🔑 Delegated Calendar service
    service = get_calendar_service(user_google_email)

    logger.info(
        f"[create_event] Invoked. Email: '{user_google_email}', Summary: {summary}"
    )
    logger.info(f"[create_event] Incoming attachments param: {attachments}")

    # Normalize attachments if provided as comma-separated string
    if attachments and isinstance(attachments, str):
        attachments = [a.strip() for a in attachments.split(",") if a.strip()]

    event_body: Dict[str, Any] = {
        "summary": summary,
        "start": (
            {"date": start_time}
            if "T" not in start_time
            else {"dateTime": start_time}
        ),
        "end": (
            {"date": end_time}
            if "T" not in end_time
            else {"dateTime": end_time}
        ),
    }

    if description:
        event_body["description"] = description
    if location:
        event_body["location"] = location
    if timezone:
        if "dateTime" in event_body["start"]:
            event_body["start"]["timeZone"] = timezone
        if "dateTime" in event_body["end"]:
            event_body["end"]["timeZone"] = timezone
    if attendees:
        event_body["attendees"] = [{"email": email} for email in attendees]

    # --- Reminders ---
    if reminders is not None or not use_default_reminders:
        reminder_data = {
            "useDefault": use_default_reminders and reminders is None
        }
        if reminders is not None:
            validated = _parse_reminders_json(reminders, "create_event")
            if validated:
                reminder_data["overrides"] = validated
                reminder_data["useDefault"] = False
        event_body["reminders"] = reminder_data

    # --- Transparency ---
    _apply_transparency_if_valid(event_body, transparency, "create_event")

    # --- Google Meet ---
    if add_google_meet:
        event_body["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

    # --- Attachments ---
    if attachments:
        event_body["attachments"] = []
        drive_service = None
        try:
            drive_service = service._http and build("drive", "v3", http=service._http)
        except Exception:
            drive_service = None

        for att in attachments:
            file_id = None
            if att.startswith("https://"):
                match = re.search(r"(?:/d/|/file/d/|id=)([\w-]+)", att)
                file_id = match.group(1) if match else None
            else:
                file_id = att

            if not file_id:
                continue

            title = "Drive Attachment"
            mime_type = "application/vnd.google-apps.drive-sdk"

            if drive_service:
                try:
                    meta = await asyncio.to_thread(
                        lambda: drive_service.files()
                        .get(
                            fileId=file_id,
                            fields="mimeType,name",
                            supportsAllDrives=True,
                        )
                        .execute()
                    )
                    mime_type = meta.get("mimeType", mime_type)
                    title = meta.get("name", title)
                except Exception:
                    pass

            event_body["attachments"].append(
                {
                    "fileUrl": f"https://drive.google.com/open?id={file_id}",
                    "title": title,
                    "mimeType": mime_type,
                }
            )

    created_event = await asyncio.to_thread(
        lambda: service.events()
        .insert(
            calendarId=calendar_id,
            body=event_body,
            supportsAttachments=bool(attachments),
            conferenceDataVersion=1 if add_google_meet else 0,
        )
        .execute()
    )

    link = created_event.get("htmlLink", "No link available")

    confirmation_message = (
        f"Successfully created event "
        f"'{created_event.get('summary', summary)}' "
        f"for {user_google_email}. Link: {link}"
    )

    if add_google_meet and "conferenceData" in created_event:
        for ep in created_event["conferenceData"].get("entryPoints", []):
            if ep.get("entryPointType") == "video":
                confirmation_message += f" Google Meet: {ep.get('uri')}"
                break

    logger.info(
        f"Event created successfully for {user_google_email}. "
        f"ID: {created_event.get('id')}"
    )
    return confirmation_message


@server.tool()
@handle_http_errors("modify_event", service_type="calendar")
@require_authentik()
async def modify_event(
    request,
    user,
    event_id: str,
    calendar_id: str = "primary",
    summary: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    timezone: Optional[str] = None,
    add_google_meet: Optional[bool] = None,
    reminders: Optional[Union[str, List[Dict[str, Any]]]] = None,
    use_default_reminders: Optional[bool] = None,
    transparency: Optional[str] = None,
) -> str:
    """
    Modifies an existing Google Calendar event using Authentik for identity
    and Google Workspace domain-wide delegation for API access.
    """

    # 🔐 Identity from Authentik
    user_google_email = user.email

    # 🔑 Delegated Calendar service
    service = get_calendar_service(user_google_email)

    logger.info(
        f"[modify_event] Invoked. Email: '{user_google_email}', Event ID: {event_id}"
    )

    # Build the event body with only the fields that are provided
    event_body: Dict[str, Any] = {}

    if summary is not None:
        event_body["summary"] = summary

    if start_time is not None:
        event_body["start"] = (
            {"date": start_time}
            if "T" not in start_time
            else {"dateTime": start_time}
        )
        if timezone and "dateTime" in event_body["start"]:
            event_body["start"]["timeZone"] = timezone

    if end_time is not None:
        event_body["end"] = (
            {"date": end_time}
            if "T" not in end_time
            else {"dateTime": end_time}
        )
        if timezone and "dateTime" in event_body["end"]:
            event_body["end"]["timeZone"] = timezone

    if description is not None:
        event_body["description"] = description

    if location is not None:
        event_body["location"] = location

    if attendees is not None:
        event_body["attendees"] = [{"email": email} for email in attendees]

    # --- Reminders ---
    if reminders is not None or use_default_reminders is not None:
        reminder_data: Dict[str, Any] = {}

        if use_default_reminders is not None:
            reminder_data["useDefault"] = use_default_reminders
        else:
            try:
                existing_event = await asyncio.to_thread(
                    lambda: service.events()
                    .get(calendarId=calendar_id, eventId=event_id)
                    .execute()
                )
                reminder_data["useDefault"] = (
                    existing_event.get("reminders", {}).get("useDefault", True)
                )
            except Exception as e:
                logger.warning(
                    f"[modify_event] Could not fetch existing reminders: {e}"
                )
                reminder_data["useDefault"] = True

        if reminders is not None:
            validated = _parse_reminders_json(reminders, "modify_event")
            if validated:
                reminder_data["overrides"] = validated
                reminder_data["useDefault"] = False

        event_body["reminders"] = reminder_data

    # --- Transparency ---
    _apply_transparency_if_valid(event_body, transparency, "modify_event")

    if not event_body:
        raise Exception("No fields provided to modify the event.")

    logger.info(
        f"[modify_event] Updating event '{event_id}' in calendar '{calendar_id}'"
    )

    # Fetch existing event to preserve fields
    try:
        existing_event = await asyncio.to_thread(
            lambda: service.events()
            .get(calendarId=calendar_id, eventId=event_id)
            .execute()
        )

        _preserve_existing_fields(
            event_body,
            existing_event,
            {
                "summary": summary,
                "description": description,
                "location": location,
                "attendees": attendees,
            },
        )

        # --- Google Meet handling ---
        if add_google_meet is not None:
            if add_google_meet:
                event_body["conferenceData"] = {
                    "createRequest": {
                        "requestId": str(uuid.uuid4()),
                        "conferenceSolutionKey": {
                            "type": "hangoutsMeet"
                        },
                    }
                }
            else:
                event_body["conferenceData"] = {}
        elif "conferenceData" in existing_event:
            event_body["conferenceData"] = existing_event["conferenceData"]

    except HttpError as e:
        if e.resp.status == 404:
            raise Exception(
                f"Event '{event_id}' not found in calendar '{calendar_id}'."
            )
        logger.warning(
            f"[modify_event] Pre-update verification error: {e}"
        )

    # --- Update event ---
    updated_event = await asyncio.to_thread(
        lambda: service.events()
        .update(
            calendarId=calendar_id,
            eventId=event_id,
            body=event_body,
            conferenceDataVersion=1,
        )
        .execute()
    )

    link = updated_event.get("htmlLink", "No link available")

    confirmation_message = (
        f"Successfully modified event "
        f"'{updated_event.get('summary', summary)}' "
        f"(ID: {event_id}) for {user_google_email}. "
        f"Link: {link}"
    )

    if add_google_meet is True and "conferenceData" in updated_event:
        for ep in updated_event["conferenceData"].get("entryPoints", []):
            if ep.get("entryPointType") == "video":
                confirmation_message += f" Google Meet: {ep.get('uri')}"
                break
    elif add_google_meet is False:
        confirmation_message += " (Google Meet removed)"

    logger.info(
        f"[modify_event] Event modified successfully for {user_google_email}. "
        f"ID: {updated_event.get('id')}"
    )

    return confirmation_message

@server.tool()
@handle_http_errors("delete_event", service_type="calendar")
@require_authentik()
async def delete_event(
    request,
    user,
    event_id: str,
    calendar_id: str = "primary",
) -> str:
    """
    Deletes an existing Google Calendar event using Authentik for identity
    and Google Workspace domain-wide delegation for API access.
    """

    # 🔐 Identity from Authentik
    user_google_email = user.email

    # 🔑 Delegated Calendar service
    service = get_calendar_service(user_google_email)

    logger.info(
        f"[delete_event] Invoked. Email: '{user_google_email}', Event ID: {event_id}"
    )

    logger.info(
        f"[delete_event] Attempting to delete event '{event_id}' "
        f"in calendar '{calendar_id}'"
    )

    # --- Verify event exists ---
    try:
        await asyncio.to_thread(
            lambda: service.events()
            .get(calendarId=calendar_id, eventId=event_id)
            .execute()
        )
        logger.info(
            "[delete_event] Event verified successfully before deletion"
        )
    except HttpError as e:
        if e.resp.status == 404:
            raise Exception(
                f"Event with ID '{event_id}' not found in calendar '{calendar_id}'."
            )
        logger.warning(
            f"[delete_event] Pre-delete verification error: {e}"
        )

    # --- Delete event ---
    await asyncio.to_thread(
        lambda: service.events()
        .delete(calendarId=calendar_id, eventId=event_id)
        .execute()
    )

    confirmation_message = (
        f"Successfully deleted event (ID: {event_id}) "
        f"from calendar '{calendar_id}' for {user_google_email}."
    )

    logger.info(
        f"[delete_event] Event deleted successfully for {user_google_email}. "
        f"ID: {event_id}"
    )

    return confirmation_message
