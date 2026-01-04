"""
Google Chat MCP Tools

This module provides MCP tools for interacting with Google Chat API.
"""

import logging
import asyncio
from typing import Optional

from google.chat_service import get_chat_service
from core.server import server
from core.utils import handle_http_errors

logger = logging.getLogger(__name__)


@server.tool()
@handle_http_errors("list_spaces", service_type="chat")
async def list_spaces(
    context,
    page_size: int = 100,
    space_type: str = "all"  # "all", "room", "dm"
) -> str:
    """
    Lists Google Chat spaces (rooms and direct messages)
    accessible to the authenticated user.
    """

    # 🔐 User injected by Authentik middleware
    user = context.user
    user_email = user.get("email")

    # Build Google Chat service for this user
    service = get_chat_service(user)

    logger.info(f"[list_spaces] User={user_email}, Type={space_type}")

    filter_param = None
    if space_type == "room":
        filter_param = "spaceType = SPACE"
    elif space_type == "dm":
        filter_param = "spaceType = DIRECT_MESSAGE"

    params = {"pageSize": page_size}
    if filter_param:
        params["filter"] = filter_param

    response = await asyncio.to_thread(
        service.spaces().list(**params).execute
    )

    spaces = response.get("spaces", [])
    if not spaces:
        return f"No Chat spaces found for user {user_email}."

    output = [f"Found {len(spaces)} Chat spaces for {user_email}:"]
    for space in spaces:
        output.append(
            f"- {space.get('displayName', 'Unnamed Space')} "
            f"(ID: {space.get('name')}, Type: {space.get('spaceType', 'UNKNOWN')})"
        )

    return "\n".join(output)


@server.tool()
@handle_http_errors("get_messages", service_type="chat")
async def get_messages(
    context,
    space_id: str,
    page_size: int = 50,
    order_by: str = "createTime desc"
) -> str:
    """
    Retrieves messages from a Google Chat space
    for the authenticated user.
    """

    user = context.user
    user_email = user.get("email")

    service = get_chat_service(user)

    logger.info(f"[get_messages] Space='{space_id}', User='{user_email}'")

    space_info = await asyncio.to_thread(
        service.spaces().get(name=space_id).execute
    )
    space_name = space_info.get("displayName", "Unknown Space")

    response = await asyncio.to_thread(
        service.spaces().messages().list(
            parent=space_id,
            pageSize=page_size,
            orderBy=order_by
        ).execute
    )

    messages = response.get("messages", [])
    if not messages:
        return f"No messages found in space '{space_name}'."

    output = [f"Messages from '{space_name}' for {user_email}:\n"]

    for msg in messages:
        sender = msg.get("sender", {}).get("displayName", "Unknown Sender")
        create_time = msg.get("createTime", "Unknown Time")
        text = msg.get("text", "No text")
        msg_id = msg.get("name", "")

        output.append(f"[{create_time}] {sender}:")
        output.append(f"  {text}")
        output.append(f"  (Message ID: {msg_id})\n")

    return "\n".join(output)


@server.tool()
@handle_http_errors("send_message", service_type="chat")
async def send_message(
    context,
    space_id: str,
    message_text: str,
    thread_key: Optional[str] = None
) -> str:
    """
    Sends a message to a Google Chat space
    as the authenticated user.
    """

    user = context.user
    user_email = user.get("email")

    service = get_chat_service(user)

    logger.info(f"[send_message] User='{user_email}', Space='{space_id}'")

    body = {"text": message_text}

    params = {
        "parent": space_id,
        "body": body
    }
    if thread_key:
        params["threadKey"] = thread_key

    message = await asyncio.to_thread(
        service.spaces().messages().create(**params).execute
    )

    msg_id = message.get("name", "")
    create_time = message.get("createTime", "")

    return (
        f"Message sent to '{space_id}' by {user_email}. "
        f"Message ID: {msg_id}, Time: {create_time}"
    )


@server.tool()
@handle_http_errors("search_messages", service_type="chat")
async def search_messages(
    context,
    query: str,
    space_id: Optional[str] = None,
    page_size: int = 25
) -> str:
    """
    Searches for messages in Google Chat spaces
    for the authenticated user.
    """

    user = context.user
    user_email = user.get("email")

    service = get_chat_service(user)

    logger.info(f"[search_messages] User='{user_email}', Query='{query}'")

    messages = []
    context_desc = ""

    if space_id:
        response = await asyncio.to_thread(
            service.spaces().messages().list(
                parent=space_id,
                pageSize=page_size,
                filter=f'text:"{query}"'
            ).execute
        )
        messages = response.get("messages", [])
        context_desc = f"space '{space_id}'"
    else:
        spaces_resp = await asyncio.to_thread(
            service.spaces().list(pageSize=50).execute
        )

        for space in spaces_resp.get("spaces", [])[:10]:
            try:
                resp = await asyncio.to_thread(
                    service.spaces().messages().list(
                        parent=space.get("name"),
                        pageSize=5,
                        filter=f'text:"{query}"'
                    ).execute
                )
                for msg in resp.get("messages", []):
                    msg["_space"] = space.get("displayName", "Unknown")
                messages.extend(resp.get("messages", []))
            except Exception:
                continue

        context_desc = "all accessible spaces"

    if not messages:
        return f"No messages found matching '{query}' in {context_desc}."

    output = [f"Found {len(messages)} messages for '{query}' in {context_desc}:"]
    for msg in messages:
        sender = msg.get("sender", {}).get("displayName", "Unknown Sender")
        text = msg.get("text", "No text")
        time = msg.get("createTime", "")
        space = msg.get("_space", "Unknown")

        if len(text) > 100:
            text = text[:100] + "..."

        output.append(f"- [{time}] {sender} in '{space}': {text}")

    return "\n".join(output)
