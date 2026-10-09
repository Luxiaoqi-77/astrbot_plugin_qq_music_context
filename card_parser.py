"""Extract bounded identity fields from QQ Music share-card segments."""
from __future__ import annotations

import json
import re
from typing import Any

from .public_facts import parse_qq_song_mid


MAX_CARD_JSON_CHARS = 32_768
MAX_CARDS_PER_EVENT = 1


def _clean(value: Any, limit: int = 120) -> str:
    """Strip controls and bound a card field before it reaches search.

    Args:
        value: Raw card metadata value.
        limit: Maximum retained character count.

    Returns:
        A compact plain-text field.
    """
    if isinstance(value, list):
        value = " / ".join(
            str(item.get("name", "")) if isinstance(item, dict) else str(item)
            for item in value
        )
    elif isinstance(value, dict):
        value = value.get("name", "")
    cleaned = re.sub(r"[\x00-\x1f\x7f<>]", " ", str(value or ""))
    return " ".join(cleaned.split())[:limit].rstrip()


def _decode_payload(payload: Any) -> dict[str, Any] | None:
    """Decode one bounded JSON wrapper without traversing arbitrary data.

    Args:
        payload: Card data from a message component.

    Returns:
        A dictionary payload, or None when it is invalid or too large.
    """
    if isinstance(payload, str):
        if len(payload) > MAX_CARD_JSON_CHARS:
            return None
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(payload, dict) and isinstance(payload.get("data"), str):
        return _decode_payload(payload["data"])
    return payload if isinstance(payload, dict) else None


def _from_payload(payload: Any, music_type: str = "") -> dict[str, str] | None:
    """Extract only QQ Music identity fields from a card payload.

    Args:
        payload: Decoded or encoded card content.
        music_type: Native music segment type when available.

    Returns:
        Whitelisted title, artist, album, and song ID fields.
    """
    data = _decode_payload(payload)
    if not data:
        return None
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    music = meta.get("music") if isinstance(meta.get("music"), dict) else None
    if music is None and isinstance(data.get("music"), dict):
        music = data["music"]
    if music is None and music_type:
        music = data
    if not isinstance(music, dict):
        return None

    tag = _clean(music.get("tag") or data.get("tag"), 40).lower()
    app = _clean(data.get("app"), 120).lower()
    raw_url = next(
        (
            str(music.get(key) or "").strip()
            for key in ("jumpUrl", "jumpurl", "url", "qqdocurl")
            if music.get(key)
        ),
        "",
    )
    mid = parse_qq_song_mid(raw_url)
    qq_music = (
        "qq音乐" in tag
        or ("qq" in app and "music" in app)
        or bool(mid)
        or music_type.strip().lower() in {"qq", "qqmusic", "qq_music"}
    )
    if not qq_music:
        return None

    raw_mid = next(
        (
            str(music.get(key) or "").strip()
            for key in ("songmid", "songMid", "mid", "id")
            if music.get(key) is not None and str(music.get(key)).strip()
        ),
        "",
    )
    if re.fullmatch(r"[A-Za-z0-9]{6,32}", raw_mid):
        mid = raw_mid
    identity = {
        "title": _clean(music.get("title") or music.get("songname") or music.get("songName")),
        "artist": _clean(
            music.get("singer")
            or music.get("artist")
            or music.get("singerName")
            or music.get("desc")
        ),
        "album": _clean(music.get("album") or music.get("albumName") or music.get("albumname")),
        "mid": mid,
    }
    if not identity["title"] or not identity["artist"]:
        return None
    return identity


def extract_qq_music_cards(event: Any) -> tuple[dict[str, str], ...]:
    """Read QQ Music JSON/native cards, skipping quoted and unrelated segments.

    Args:
        event: AstrBot message event or compatible object.

    Returns:
        Distinct card identities, capped per event.
    """
    message_obj = getattr(event, "message_obj", None)
    if message_obj is None:
        return ()
    identities = []
    seen = set()

    def add(identity: dict[str, str] | None) -> None:
        if not identity:
            return
        key = identity.get("mid") or "|".join(
            re.sub(r"[^\w]", "", identity.get(field, "").casefold())
            for field in ("title", "artist", "album")
        )
        if key and key not in seen and len(identities) < MAX_CARDS_PER_EVENT:
            seen.add(key)
            identities.append(identity)

    for component in getattr(message_obj, "message", ()) or ():
        component_name = component.__class__.__name__
        if component_name == "Reply":
            continue
        if component_name == "Json" and hasattr(component, "data"):
            add(_from_payload(component.data))
        elif component_name == "Music":
            add(
                _from_payload(
                    {
                        "id": getattr(component, "id", ""),
                        "url": getattr(component, "url", ""),
                        "title": getattr(component, "title", ""),
                        "desc": getattr(component, "content", ""),
                    },
                    music_type=str(getattr(component, "_type", "")),
                )
            )

    raw_message = getattr(message_obj, "raw_message", None)
    raw_chain = raw_message.get("message", []) if isinstance(raw_message, dict) else []
    if isinstance(raw_chain, list):
        for segment in raw_chain:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type", "")).lower()
            data = segment.get("data") or {}
            if not isinstance(data, dict):
                continue
            if segment_type == "json":
                add(_from_payload(data))
            elif segment_type == "music":
                add(_from_payload(data, music_type=str(data.get("type", ""))))
    return tuple(identities)
