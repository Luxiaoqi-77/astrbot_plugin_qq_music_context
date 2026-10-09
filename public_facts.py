"""Resolve small, source-backed facts for explicit QQ Music card identities."""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from importlib import import_module
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


_QQ_HOSTS = {"y.qq.com", "www.y.qq.com", "i.y.qq.com", "c.y.qq.com", "c6.y.qq.com"}
_SOURCE_HOSTS = {
    "y.qq.com",
    "www.y.qq.com",
    "i.y.qq.com",
    "music.apple.com",
    "open.spotify.com",
    "music.163.com",
    "baike.baidu.com",
    "wapbaike.baidu.com",
    "m.baike.com",
    "www.baike.com",
    "musicbrainz.org",
    "www.musicbrainz.org",
}
_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{6,32}$")
_UNKNOWN = "unknown（未找到可核验的公开字段）"
_MAX_EVIDENCE_CHARS = 240
_SUMMARY_CUE_PATTERN = r"歌曲|作品|音乐|专辑|创作|曲风|风格|演唱|作曲|发行|收录"


@dataclass(frozen=True)
class SongRef:
    """A bounded identity read from an explicit music card."""

    title: str
    artist: str
    album: str = ""
    mid: str = ""

    @classmethod
    def from_mapping(cls, value: Any) -> SongRef:
        """Normalize identity fields while excluding controls and query syntax.

        Args:
            value: Card metadata mapping.

        Returns:
            A bounded song identity; missing or invalid values become empty.
        """
        if not isinstance(value, dict):
            return cls("", "")

        def clean(raw: Any, limit: int) -> str:
            if not isinstance(raw, str):
                return ""
            raw = re.sub(r'[\x00-\x1f\x7f<>\[\]{}"`\\]', " ", raw)
            cleaned = " ".join(raw.split()).strip(" '\t\r\n")[:limit]
            return "" if cleaned.casefold() in {"unknown", "null", "none", "未提供"} else cleaned

        raw_mid = clean(value.get("mid"), 32)
        return cls(
            title=clean(value.get("title"), 120),
            artist=clean(value.get("artist"), 120),
            album=clean(value.get("album"), 120),
            mid=raw_mid if _ID_PATTERN.fullmatch(raw_mid) else "",
        )

    def as_dict(self) -> dict[str, str]:
        """Return the identity in the plugin's serialized shape.

        Returns:
            Title, artist, album, and optional song ID fields.
        """
        return {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "mid": self.mid,
        }

@dataclass(frozen=True)
class _VerifiedPageExcerpt:
    """A short page excerpt whose identity was checked against its HTML."""

    title: str
    url: str
    snippet: str
    matched_title: str
    matched_artist: str



def parse_qq_song_mid(url: Any) -> str:
    """Read a QQ song identifier without following or requesting the URL.

    Args:
        url: A URL supplied by a card.

    Returns:
        A valid identifier for a recognized QQ Music URL, or an empty string.
    """
    if not isinstance(url, str) or len(url) > 2048:
        return ""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower().rstrip(".")
        if (
            parts.scheme.lower() != "https"
            or host not in _QQ_HOSTS
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
            or parts.fragment
        ):
            return ""
        if host in {"c.y.qq.com", "c6.y.qq.com"}:
            if parts.path != "/base/fcgi-bin/u":
                return ""
            token = (parse_qs(parts.query, max_num_fields=8).get("__") or [""])[0]
            return token if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", token) else ""
        path_match = re.fullmatch(r"/n/ryqq/songDetail/([A-Za-z0-9]{6,32})/?", parts.path)
        if path_match:
            return path_match.group(1)
        query = parse_qs(parts.query, max_num_fields=8)
        candidate = next(
            (query.get(key, [""])[0] for key in ("songmid", "songMid", "mid") if query.get(key)),
            "",
        )
        return candidate if _ID_PATTERN.fullmatch(candidate) else ""
    except ValueError:
        return ""


def safe_source_url(value: Any) -> str:
    """Canonicalize a direct HTTPS URL from a fixed set of public sources.

    Args:
        value: URL from a normalized search result.

    Returns:
        A sanitized source URL or an empty string.
    """
    if not isinstance(value, str) or not value or len(value) > 500:
        return ""
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.casefold() != "https" or "@" in parts.netloc:
            return ""
        host = (parts.hostname or "").casefold().rstrip(".")
        if host not in _SOURCE_HOSTS or not parts.path.startswith("/"):
            return ""
        if parts.port not in (None, 443):
            return ""

        kept_parameters = []
        for key, parameter in parse_qsl(parts.query, max_num_fields=8):
            normalized_key = key.casefold()
            if normalized_key in {"songmid", "mid", "id", "i"} and re.fullmatch(
                r"[A-Za-z0-9_-]{1,80}", parameter
            ):
                kept_parameters.append((normalized_key, parameter))
        return urlunsplit(("https", host, parts.path, urlencode(kept_parameters), ""))
    except ValueError:
        return ""


def _plain_text(value: Any, limit: int) -> str:
    """Normalize a bounded public text field.

    Args:
        value: Candidate text.
        limit: Maximum output length.

    Returns:
        Normalized plain text or an empty string.
    """
    if not isinstance(value, str):
        return ""
    return " ".join(re.sub(r"[\x00-\x1f\x7f<>\[\]{}]", " ", value).split())[:limit]


def _compact(value: str) -> str:
    """Build a comparison key while preserving Unicode letters and digits.

    Args:
        value: Text to normalize.

    Returns:
        A compact comparison key.
    """
    folded = value.casefold()
    return "".join(character for character in folded if character == "_" or character.isalnum())


def _source_record(item: Any, song: SongRef) -> dict[str, str] | None:
    """Check URL and work identity without applying recording-version rules.

    Args:
        item: A normalized AnySearch result.
        song: Requested title and artist.

    Returns:
        A short sanitized source record when both title and artist match.
    """
    if isinstance(item, dict):
        title, url = item.get("title"), item.get("url")
        snippet = item.get("snippet") or item.get("content") or ""
    else:
        title = getattr(item, "title", "")
        url = getattr(item, "url", "")
        snippet = getattr(item, "snippet", "") or getattr(item, "content", "")
    source_url = safe_source_url(url)
    source_title = _plain_text(title, 160)
    source_snippet = _plain_text(snippet, _MAX_EVIDENCE_CHARS)
    searchable = _compact(f"{source_title} {source_snippet}")
    song_title = _compact(song.title)
    artists = [
        _compact(name)
        for name in re.split(r"[/／、,，&和]+", song.artist)
        if _compact(name)
    ]
    page_identity_verified = (
        isinstance(item, _VerifiedPageExcerpt)
        and _compact(item.matched_title) == song_title
        and _compact(item.matched_artist) in artists
    )
    title_matches = song_title in searchable or (
        page_identity_verified and _compact(item.matched_title) == song_title
    )
    artist_matches = any(len(name) >= 2 and name in searchable for name in artists)
    if not artist_matches and page_identity_verified:
        artist_matches = True
    if not source_url or len(song_title) < 2 or not title_matches or not artist_matches:
        return None
    location = urlsplit(source_url)
    source_host = (location.hostname or "").lower()
    source_query = parse_qs(location.query, max_num_fields=8)
    if (
        source_host == "music.apple.com"
        and "/album/" in location.path.lower()
        and not source_query.get("i")
    ) or (
        source_host == "open.spotify.com"
        and "/album/" in location.path.lower()
    ):
        return None
    return {
        "title": source_title or (urlsplit(source_url).hostname or ""),
        "url": source_url,
        "snippet": source_snippet,
        "confidence": (
            "high"
            if source_host
            in {
                "y.qq.com",
                "www.y.qq.com",
                "i.y.qq.com",
                "music.apple.com",
                "open.spotify.com",
                "music.163.com",
            }
            else "medium"
        ),
    }


def _album_is_named(text: str, album: str) -> bool:
    """Check for an explicit album relationship in source prose.

    Args:
        text: Short public source summary.
        album: Album label from the card.

    Returns:
        True when the summary associates the track with this album.
    """
    if not album:
        return False
    escaped = re.escape(album)
    return bool(
        re.search(
            rf"(?:专辑|album)\s*[《“\"]?{escaped}[》”\"]?|"
            rf"(?:收录于|收录在|included\s+(?:in|on))\s*[《“\"]?{escaped}[》”\"]?",
            text,
            re.IGNORECASE,
        )
    )


def _title_names_other_recording(source_title: str, song_title: str) -> bool:
    """Detect explicit alternate-recording labels next to the song title only.

    Args:
        source_title: Public page title.
        song_title: Requested work title.

    Returns:
        True when a specific alternate version is adjacent to the title.
    """
    match = re.search(re.escape(song_title), source_title, re.IGNORECASE)
    if not match:
        return False
    marker = (
        r"现场(?:演唱|录音|演出)?版|演唱会版|不插电(?:版)?|混音版|翻唱(?:版)?|"
        r"伴奏(?:版)?|重制版|remix(?:\s*(?:version|ver|版))?|"
        r"live(?:\s*(?:version|ver|版))?|cover(?:\s*(?:version|ver|版))?|"
        r"demo(?:\s*(?:version|ver|版))?|acoustic(?:\s*(?:version|ver|版))?|"
        r"instrumental|remaster(?:ed)?(?:\s*(?:version|ver|版))?"
    )
    separators = r"[\s\-—–:：,，()（）\[\]【】《》「」“”'\".]*"
    before = source_title[max(0, match.start() - 36) : match.start()]
    after = source_title[match.end() : match.end() + 36]
    return bool(
        re.search(rf"(?:{marker}){separators}$", before, re.IGNORECASE)
        or re.match(rf"^{separators}(?:{marker})", after, re.IGNORECASE)
    )


_GENRE_TERMS = (
    ("嘻哈说唱", r"嘻哈说唱|hip[\s-]*hop(?:\s+rap)?"),
    ("流行摇滚", r"流行摇滚|pop[\s-]*rock"),
    ("民谣摇滚", r"民谣摇滚|folk[\s-]*rock"),
    ("华语流行", r"华语流行|mandopop"),
    ("另类摇滚", r"另类摇滚|alternative[\s-]*rock"),
    ("嘻哈", r"嘻哈|hip[\s-]*hop"),
    ("说唱", r"说唱|rap"),
    ("流行", r"流行|pop"),
    ("摇滚", r"摇滚|rock"),
    ("民谣", r"民谣|folk"),
    ("爵士", r"爵士|jazz"),
    ("古典", r"古典|classical"),
    ("电子", r"电子|electronic"),
    ("蓝调", r"蓝调|blues"),
    ("朋克", r"朋克|punk"),
    ("乡村", r"乡村|country"),
    ("灵魂乐", r"灵魂乐|soul"),
)


def _album_overall_style_clause(text: str) -> bool:
    """Reject genre statements whose grammatical subject is the whole album."""
    return bool(
        re.search(
            r"(?:整张|整个|整体|总体|该|这张|本)?专辑(?:的)?"
            r"(?:整体|总体|整体上)?\s*(?:曲风|风格|类型)"
            r"|专辑(?:《[^》]{1,40}》)?\s*(?:整体|总体)?(?:曲风|风格|类型)"
            r"|专辑.{0,30}的(?:整体|总体)?\s*(?:曲风|风格|类型)"
            r"|(?:the\s+)?(?:whole|overall)\s+album(?:'s)?\s+(?:genre|style)",
            text,
            re.IGNORECASE,
        )
    )


def _explicit_genre_label(text: str) -> str:
    """Return a named genre only beside an explicit genre cue or song label."""
    for label, term in _GENRE_TERMS:
        if re.search(
            rf"(?:曲风|风格|类型|genre)\s*(?:为|是|属于|定位为|[:：-])?\s*(?:{term})(?![a-z])"
            rf"|(?:{term})\s*(?:风格|曲风|音乐类型|歌曲|song|track)",
            text,
            re.IGNORECASE,
        ):
            return label
    return ""


def _song_genre_claim(
    source_title: str, source_snippet: str, song_title: str
) -> tuple[str, str]:
    """Find an explicit genre statement tied to the matched song, not its album."""
    if not song_title or _title_names_other_recording(source_title, song_title):
        return "", ""
    title_key = _compact(song_title)
    title_identifies_song = title_key in _compact(source_title)

    # Encyclopedic entry titles often encode the work's genre directly, e.g.
    # “听妈妈的话（2006年周杰伦演唱的嘻哈说唱歌曲）”.
    if title_identifies_song and not _album_overall_style_clause(source_title):
        title_genre = _explicit_genre_label(source_title)
        if title_genre:
            return title_genre, source_title[:_MAX_EVIDENCE_CHARS]

    clauses = re.findall(r"[^。！？；;\n]+(?:[。！？；;]|$)", source_snippet)
    for clause in clauses:
        clause = clause.strip()
        if not clause or _album_overall_style_clause(clause):
            continue
        genre = _explicit_genre_label(clause)
        if not genre:
            continue
        # The source record has already matched both the song and artist. A
        # song-specific page title lets its concise summary supply a direct
        # “曲风为 X” claim; broader pages must name the song in that clause.
        if title_identifies_song or title_key in _compact(clause):
            return genre, clause[:_MAX_EVIDENCE_CHARS]
    return "", ""


def _theme_from_prose(text: str) -> str:
    """Take a short semantic summary clause, never a lyric passage.

    Args:
        text: Public source summary.

    Returns:
        A short topic phrase following an explicit theme statement.
    """
    if re.search(r"(?:歌词全文|歌词如下|歌词片段|lyrics\s*[:：])", text, re.IGNORECASE):
        return ""
    patterns = (
        r"(?:歌词|歌曲|作品)?(?:的)?(?:主题|主旨|内容)\s*(?:主要)?"
        r"(?:为|是|围绕|聚焦(?:于)?|描写|讲述|表达|刻画|展现|反映|体现|诉说|赞美|探讨)"
        r"\s*([^。！？；;，,\n“”\"]{2,48})",
        r"(?:歌词|歌曲)(?:中)?(?:围绕|聚焦(?:于)?|描写|讲述|表达|刻画|展现|反映|体现|诉说|赞美|探讨)"
        r"\s*([^。！？；;，,\n“”\"]{2,48})",
        r"(?:字里行间|歌词中|歌曲中|歌中)(?:表达|流露|传达|呈现|展现|描写|体现|反映)(?:了)?"
        r"\s*([^。！？；;，,\n“”\"]{2,48})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        phrase = match.group(1).strip(" \t\r\n“”\"'《》")
        phrase = re.sub(r"^(?:他|她|歌曲|作品)(?:的)?", "", phrase).strip()
        if len(phrase) >= 2 and not re.search(r"[♪♫]", phrase):
            return phrase[:48]
    return ""


def _evidence_excerpt(text: str, field: str, album: str = "") -> str:
    """Retain only the short prose clause that supports a returned field.

    Args:
        text: Public source summary.
        field: Fact name whose evidence is being preserved.
        album: Card album used to select recording-level evidence.

    Returns:
        One bounded supporting clause, or an empty string.
    """
    clauses = re.findall(r"[^。！？；;\n]+(?:[。！？；;]|$)", text)
    if field == "歌词主题":
        marker = r"主题|主旨|内容|字里行间|歌词中|歌曲中|歌中"
    elif field == "曲风":
        marker = r"曲风|风格|类型|genre|song|track"
    else:
        marker = rf"{re.escape(album)}|主打单曲|主打曲|lead\s+single"
    for clause in clauses:
        excerpt = clause.strip()
        if re.search(marker, excerpt, re.IGNORECASE):
            if re.search(r"(?:歌词全文|歌词如下|歌词片段|lyrics\s*[:：])", excerpt, re.IGNORECASE):
                return ""
            if len(re.findall(r"[\u4e00-\u9fff]", excerpt)) > 90:
                return ""
            return excerpt[:_MAX_EVIDENCE_CHARS]
    return ""


def _intro_from_prose(text: str, album: str) -> str:
    """Describe only explicitly sourced album associations.

    Args:
        text: Public source summary.
        album: Album label from the card.

    Returns:
        A concise source-backed introduction, or an empty string.
    """
    if not _album_is_named(text, album):
        return ""
    parts = [f"公开条目将其与专辑《{album}》关联"]
    if re.search(r"主打单曲|主打曲|lead single", text, re.IGNORECASE):
        parts.append("公开条目称其为专辑主打曲")
    return "；".join(parts)


_PAGE_READ_HOSTS = {
    "baike.baidu.com",
    "wapbaike.baidu.com",
    "m.baike.com",
    "www.baike.com",
}
_PAGE_READ_MAX_BYTES = 32 * 1024


def _blank_fact_table() -> dict[str, dict[str, str]]:
    """Create independent unknown states for the three requested fields.

    Returns:
        Fresh field records; callers may safely attach source evidence later.
    """
    scopes = {
        "歌曲介绍": "recording",
        "曲风": "work",
        "歌词主题": "work",
    }
    return {
        name: {
            "value": _UNKNOWN,
            "source": "",
            "evidence": "",
            "reason": "未找到可核验的公开字段",
            "confidence": "unknown",
            "scope": scope,
        }
        for name, scope in scopes.items()
    }


def _page_evidence_excerpt(
    html_text: str, identity: Any, candidate_title: str = ""
) -> str:
    """Extract at most one short descriptive clause from a matched article page.

    Args:
        html_text: The bounded prefix read from a trusted public page.
        identity: Card title and artist used to confirm the article.
        candidate_title: Search-result title that led to this page.

    Returns:
        A short topic clause, or a non-lyric prose sentence for cautious analysis.
    """
    from html import unescape

    song = SongRef.from_mapping(identity)
    if not song.title or not song.artist or not isinstance(html_text, str):
        return ""
    page_prefix = html_text[:_PAGE_READ_MAX_BYTES]
    page_prefix = re.sub(
        r"(?is)<(script|style|noscript)\b[^>]*>.*?</\1>", " ", page_prefix
    )
    page_title_match = re.search(
        r"(?is)<title\b[^>]*>(.*?)</title\s*>", page_prefix
    )
    page_title = (
        _plain_text(
            unescape(re.sub(r"<[^>]+>", " ", page_title_match.group(1))), 160
        )
        if page_title_match
        else ""
    )
    song_key = _compact(song.title)
    artist_keys = [
        _compact(part)
        for part in re.split(r"[/／、,，&和]+", song.artist)
        if _compact(part)
    ]
    heading_match = re.search(r"(?is)<h1\b[^>]*>(.*?)</h1\s*>", page_prefix)
    page_heading = (
        _plain_text(unescape(re.sub(r"<[^>]+>", " ", heading_match.group(1))), 160)
        if heading_match
        else ""
    )
    title_context = _compact(f"{page_title} {page_heading}")
    if song_key not in title_context:
        return ""

    paragraphs = re.findall(r"(?is)<p\b[^>]*>(.*?)</p\s*>", page_prefix)
    lyric_markers = r"(?:歌词全文|歌词如下|歌词片段|歌词节选|lyrics\s*[:：])"
    topic_clause = re.compile(
        r"(?:字里行间|歌词中|歌曲中|歌中)"
        r"(?:表达|流露|传达|呈现|展现|描写|体现|反映)(?:了)?"
        r"\s*([^。！？；;，,]{2,48})"
    )
    for paragraph in paragraphs[:24]:
        text = _plain_text(
            unescape(re.sub(r"<[^>]+>", " ", paragraph)), _MAX_EVIDENCE_CHARS * 2
        )
        if not text or re.search(lyric_markers, text, re.IGNORECASE):
            continue
        normalized = _compact(text)
        if not any(len(key) >= 2 and key in normalized for key in artist_keys):
            continue
        match = topic_clause.search(text)
        if match:
            excerpt = _plain_text(match.group(0), 120)
            if (
                len(re.findall(r"[\u4e00-\u9fff]", excerpt)) <= 60
                and not re.search(r"[♪♫]", excerpt)
            ):
                return excerpt
        if (
            len(text) <= 160
            and len(re.findall(r"[\u4e00-\u9fff]", text)) <= 75
            and not re.search(r"歌词|lyrics|[♪♫]", text, re.IGNORECASE)
            and re.search(_SUMMARY_CUE_PATTERN, text)
        ):
            return text
    return ""


def _recording_source_candidate(
    item: Any, identity: dict[str, str]
) -> dict[str, str] | None:
    """Match a particular recording only when its card album is evidenced.

    Args:
        item: AnySearch result or a normalized page excerpt.
        identity: Song title, artist, and optional card album.

    Returns:
        A recording-level source or None when edition identity is incomplete.
    """
    song = SongRef.from_mapping(identity)
    source = _source_record(item, song)
    if source is None or not song.album:
        return None
    if not _album_is_named(source["snippet"], song.album):
        return None
    if _title_names_other_recording(source["title"], song.title):
        return None
    return source


def interpret_results(
    results: Any, identity: dict[str, str]
) -> dict[str, dict[str, str]]:
    """Separate source-supported work facts from recording-specific facts.

    Args:
        results: Bounded AnySearch items plus, when needed, one safe page excerpt.
        identity: Song identity read from the card.

    Returns:
        Source-backed field records and optional explicitly unclassified prose.
    """
    facts = _blank_fact_table()
    song = SongRef.from_mapping(identity)
    if not song.title or not song.artist or not isinstance(results, (list, tuple)):
        return facts

    work_sources = []
    recording_found = False
    for item in results[:8]:
        work = _source_record(item, song)
        if work:
            work_sources.append(work)
            if facts["歌词主题"]["confidence"] == "unknown":
                topic = _theme_from_prose(work["snippet"])
                excerpt = _evidence_excerpt(work["snippet"], "歌词主题")
                if topic and excerpt:
                    facts["歌词主题"] = {
                        "value": topic,
                        "source": work["url"],
                        "evidence": excerpt,
                        "reason": "",
                        "confidence": work["confidence"],
                        "scope": "work",
                    }
            if facts["曲风"]["confidence"] == "unknown":
                genre, evidence = _song_genre_claim(
                    work["title"], work["snippet"], song.title
                )
                if genre and evidence:
                    recording_for_genre = (
                        _recording_source_candidate(item, song.as_dict())
                        if song.album
                        else None
                    )
                    genre_source = recording_for_genre or work
                    facts["曲风"] = {
                        "value": genre,
                        "source": genre_source["url"],
                        "evidence": evidence,
                        "reason": "",
                        "confidence": genre_source["confidence"],
                        "scope": "recording" if recording_for_genre else "work",
                    }

        recording = _recording_source_candidate(item, song.as_dict()) if song.album else None
        if not recording:
            continue
        recording_found = True
        value = _intro_from_prose(recording["snippet"], song.album)
        if value and facts["歌曲介绍"]["confidence"] == "unknown":
            excerpt = _evidence_excerpt(recording["snippet"], "歌曲介绍", song.album)
            if excerpt:
                facts["歌曲介绍"] = {
                    "value": value,
                    "source": recording["url"],
                    "evidence": excerpt,
                    "reason": "",
                    "confidence": recording["confidence"],
                    "scope": "recording",
                }

    if not recording_found:
        facts["歌曲介绍"]["reason"] = (
            "卡片没有可核验的专辑/版本；录音级字段保持 unknown"
            if not song.album
            else "没有同时匹配歌名、歌手、专辑/版本的来源"
        )
    elif facts["歌曲介绍"]["confidence"] == "unknown":
        facts["歌曲介绍"]["reason"] = "匹配录音来源没有明确支持该字段的表述"
    if facts["曲风"]["confidence"] == "unknown":
        facts["曲风"]["reason"] = (
            "匹配歌名/歌手来源没有明确说明歌曲曲风；不把心情形容或专辑整体风格当作歌曲曲风"
            if work_sources
            else "没有同时匹配歌名与歌手的作品级来源"
        )

    if facts["歌词主题"]["confidence"] == "unknown":
        facts["歌词主题"]["reason"] = (
            "匹配的歌名/歌手来源没有明确主题表述"
            if work_sources
            else "没有同时匹配歌名与歌手的作品级来源"
        )
        for candidate in work_sources:
            text = candidate["snippet"]
            if re.search(
                r"(?:歌词全文|歌词如下|歌词片段|歌词节选|lyrics\s*[:：])",
                text,
                re.IGNORECASE,
            ):
                continue
            for clause in re.findall(r"[^。！？；;\n]+(?:[。！？；;]|$)", text):
                excerpt = clause.strip()
                if (
                    not excerpt
                    or len(excerpt) > 160
                    or len(re.findall(r"[\u4e00-\u9fff]", excerpt)) > 75
                    or re.search(r"[♪♫]|歌词|lyrics", excerpt, re.IGNORECASE)
                    or not re.search(
                        _SUMMARY_CUE_PATTERN, excerpt
                    )
                ):
                    continue
                facts["待模型分析的来源摘要"] = {
                    "value": "未分类来源摘要（待模型谨慎分析）",
                    "source": candidate["url"],
                    "evidence": excerpt,
                    "reason": (
                        "歌名与歌手已匹配；解析器未将此短摘要归类为已验证字段。"
                        "仅供模型谨慎分析，不代表已确认的歌词主题或听觉事实。"
                    ),
                    "confidence": "unclassified",
                    "scope": "work",
                }
                break
            if "待模型分析的来源摘要" in facts:
                break
    return facts


def _identity_token(song: SongRef) -> str:
    """Return an event-independent key from the public card identity."""
    return ":".join(_compact(value) for value in (song.title, song.artist, song.album))


class QQMusicPublicFacts:
    """Resolve a card identity using one anonymous search and optional page read."""

    _SEARCH_TIMEOUT = 8
    _SEARCH_SPACING = 1.0
    _FACT_CACHE_TTL = 12 * 60 * 60
    _EMPTY_CACHE_TTL = 5 * 60
    _PAGE_READ_TIMEOUT = 4
    _MAX_CACHED_SONGS = 128

    def __init__(self):
        """Prepare per-instance cache and task coordination."""
        self._facts_by_key: dict[str, tuple[float, dict[str, dict[str, str]]]] = {}
        self._pending_lookups: dict[str, asyncio.Task] = {}
        self._state_lock = asyncio.Lock()
        self._search_gate = asyncio.Lock()
        self._last_search_started = 0.0

    @staticmethod
    def recording_source_candidate(
        item: Any, identity: dict[str, str]
    ) -> dict[str, str] | None:
        """Apply card album and version evidence to a matched result.

        Args:
            item: AnySearch result or page excerpt.
            identity: Card identity.

        Returns:
            A recording-specific source, if the card edition is established.
        """
        return _recording_source_candidate(item, identity)

    async def search_card_identity(self, identity: dict[str, str]) -> list[Any]:
        """Search fixed metadata intents using only public card identity.

        Args:
            identity: Bounded title, artist, and optional album from the card.

        Returns:
            At most five normalized results; failures and timeouts yield none.
        """
        song = SongRef.from_mapping(identity)
        terms = [part for part in (song.title, song.artist, song.album) if part]
        query = " ".join(
            [f'"{part}"' for part in terms]
            + ["曲风", "歌曲介绍", "歌词主题", "创作背景"]
        )[:500]
        request = dict(
            (name, value)
            for name, value in (
                ("query", query),
                ("max_results", 5),
                ("format", "json"),
                ("zone", "cn"),
                ("language", "zh-CN"),
            )
        )
        try:
            search = getattr(
                import_module("astrbot.core.tools.web_search_tools"),
                "_anysearch_search",
            )
            async with self._search_gate:
                current = time.monotonic()
                remaining = self._SEARCH_SPACING - (current - self._last_search_started)
                if remaining > 0:
                    await asyncio.sleep(remaining)
                self._last_search_started = time.monotonic()
                response = await asyncio.wait_for(
                    search({"websearch_anysearch_key": []}, request),
                    timeout=self._SEARCH_TIMEOUT,
                )
        except Exception:
            return []
        return response[:5] if isinstance(response, list) else []

    async def read_search_result_excerpt(
        self, candidate: dict[str, str], identity: dict[str, str]
    ) -> _VerifiedPageExcerpt | None:
        """Follow one matching allow-listed Baike result for a short page excerpt.

        Args:
            candidate: A result already matched to the card identity.
            identity: Card identity that the fetched article must confirm.

        Returns:
            One short sourced excerpt, or None when the page is unsafe/unusable.
        """
        import aiohttp

        current_url = safe_source_url(candidate.get("url"))
        if not current_url:
            return None
        first_host = (urlsplit(current_url).hostname or "").lower()
        if first_host not in _PAGE_READ_HOSTS:
            return None

        timeout = aiohttp.ClientTimeout(
            total=self._PAGE_READ_TIMEOUT, connect=2, sock_read=3
        )
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Range": f"bytes=0-{_PAGE_READ_MAX_BYTES - 1}",
            "User-Agent": "QQMusicContext/0.1 (+public metadata excerpt)",
        }
        async with aiohttp.ClientSession(
            timeout=timeout,
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
        ) as session:
            for _ in range(3):
                async with session.get(
                    current_url, headers=headers, allow_redirects=False
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            return None
                        target = safe_source_url(urljoin(current_url, location))
                        target_host = (urlsplit(target).hostname or "").lower()
                        if not target or target_host not in _PAGE_READ_HOSTS:
                            return None
                        current_url = target
                        continue
                    content_type = response.headers.get("Content-Type", "").lower()
                    if response.status not in {200, 206} or "html" not in content_type:
                        return None
                    page_bytes = await response.content.read(_PAGE_READ_MAX_BYTES)
                    charset = response.charset or "utf-8"
                    page_html = page_bytes.decode(charset, errors="replace")
                    excerpt = _page_evidence_excerpt(
                        page_html, identity, candidate.get("title", "")
                    )
                    if not excerpt:
                        return None
                    page_title = re.search(
                        r"(?is)<title\b[^>]*>(.*?)</title\s*>", page_html
                    )
                    from html import unescape

                    verified_title = (
                        _plain_text(
                            unescape(re.sub(r"<[^>]+>", " ", page_title.group(1))), 160
                        )
                        if page_title
                        else candidate.get("title", "")
                    )
                    song = SongRef.from_mapping(identity)
                    return _VerifiedPageExcerpt(
                        title=verified_title,
                        url=current_url,
                        snippet=excerpt,
                        matched_title=song.title,
                        matched_artist=song.artist,
                    )
        return None

    async def public_facts_for_card(
        self, card_identity: Any
    ) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
        """Return fresh cached facts or join the active lookup for this song."""
        song = SongRef.from_mapping(card_identity)
        identity = song.as_dict()
        if not song.title or not song.artist:
            return identity, _blank_fact_table()

        key = _identity_token(song)
        now = time.monotonic()
        async with self._state_lock:
            cached = self._facts_by_key.get(key)
            if cached:
                lifetime = (
                    self._FACT_CACHE_TTL
                    if any(
                        value.get("source")
                        and value.get("confidence") != "unknown"
                        for value in cached[1].values()
                    )
                    else self._EMPTY_CACHE_TTL
                )
                if now - cached[0] < lifetime:
                    import copy

                    return identity, copy.deepcopy(cached[1])
            task = self._pending_lookups.get(key)
            if task is None:
                task = asyncio.create_task(self._populate_card_facts(key, identity))
                self._pending_lookups[key] = task

        facts = await asyncio.shield(task)
        import copy

        return identity, copy.deepcopy(facts)

    async def _populate_card_facts(
        self, key: str, identity: dict[str, str]
    ) -> dict[str, dict[str, str]]:
        """Run one search, optionally read one matching page, and cache the result."""
        try:
            results = await self.search_card_identity(identity)
            facts = interpret_results(results, identity)
            if facts["歌词主题"]["confidence"] == "unknown":
                song = SongRef.from_mapping(identity)
                candidate = None
                for item in results:
                    matched = _source_record(item, song)
                    if not matched:
                        continue
                    host = (urlsplit(matched["url"]).hostname or "").lower()
                    if host in _PAGE_READ_HOSTS:
                        candidate = matched
                        break
                if candidate:
                    page_result = await self.read_search_result_excerpt(
                        candidate, identity
                    )
                    if page_result:
                        facts = interpret_results([*results, page_result], identity)
        except asyncio.CancelledError:
            raise
        except Exception:
            facts = _blank_fact_table()

        try:
            async with self._state_lock:
                if len(self._facts_by_key) >= self._MAX_CACHED_SONGS:
                    self._facts_by_key.pop(next(iter(self._facts_by_key)))
                self._facts_by_key[key] = (time.monotonic(), facts)
        finally:
            current = asyncio.current_task()
            async with self._state_lock:
                if self._pending_lookups.get(key) is current:
                    self._pending_lookups.pop(key, None)
        return facts

