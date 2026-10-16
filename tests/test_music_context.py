from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
STAGING_ROOT = PLUGIN_ROOT.parent
ASTRBOT_ROOT = os.environ.get("ASTRBOT_ROOT")
if ASTRBOT_ROOT:
    sys.path.insert(0, ASTRBOT_ROOT)
for plugin_path in (STAGING_ROOT,):
    sys.path.insert(0, str(plugin_path))

from astrbot.core.platform.message_type import MessageType
from astrbot.core.provider.entities import ProviderRequest
from astrbot_plugin_continuous_message.event_context import (
    EVENT_CONTEXT_APPLIED_EXTRA_KEY,
    EVENT_CONTEXT_EXTRA_KEY,
    EVENT_CONTEXT_INJECTED_EXTRA_KEY,
    normalize_context_blocks,
)
from astrbot_plugin_continuous_message.main import ContinuousMessagePlugin
from astrbot_plugin_qq_music_context.card_parser import extract_qq_music_cards
from astrbot_plugin_qq_music_context.main import QQMusicContextPlugin
from astrbot_plugin_qq_music_context.public_facts import (
    QQMusicPublicFacts,
    interpret_results,
    _page_evidence_excerpt,
    _VerifiedPageExcerpt,
    parse_qq_song_mid,
    safe_source_url,
)


FIXTURE_PATH = PLUGIN_ROOT / "tests" / "fixtures" / "public_theme_replay.json"
LIVE_REPLAY_PATH = (
    PLUGIN_ROOT / "tests" / "fixtures" / "host_anysearch_provider_request_replay.json"
)
FINAL_ACCEPTANCE_PATH = (
    PLUGIN_ROOT / "tests" / "fixtures" / "automatic_anysearch_acceptance_final.json"
)


class Event:
    def __init__(self, *, message=None, raw_message=None, message_type=MessageType.FRIEND_MESSAGE):
        self.message_obj = SimpleNamespace(message=message or [], raw_message=raw_message or {})
        self.message_type = message_type
        self.unified_msg_origin = "test:friend:session"
        self.message_str = ""
        self.extras = {}
        self.media = []

    def get_message_type(self):
        return self.message_type

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


class Json:
    def __init__(self, data):
        self.data = data


def make_card_payload():
    return {
        "app": "com.tencent.structmsg",
        "meta": {
            "music": {
                "tag": "QQ音乐",
                "title": "听妈妈的话",
                "singer": "周杰伦",
                "jumpUrl": "https://y.qq.com/n/ryqq/songDetail/0034MnYb0qxYhV",
            }
        },
    }


class PublicThemeReplayTests(unittest.TestCase):
    def test_single_live_host_replay_keeps_all_fields_unknown(self):
        replay = json.loads(LIVE_REPLAY_PATH.read_text(encoding="utf-8"))
        facts = interpret_results(
            [], replay["retrieval"]["query_identity"]
        )

        self.assertEqual(replay["retrieval"]["helper_calls"], 1)
        self.assertTrue(replay["retrieval"]["helper_returned_normally"])
        self.assertEqual(replay["retrieval"]["normalized_result_count"], 5)
        self.assertTrue(all(item["confidence"] == "unknown" for item in facts.values()))
        self.assertEqual(replay["field_results"]["曲风"]["confidence"], "unknown")
        self.assertIsNone(replay["source_evidence"])
        self.assertEqual(replay["provider_request_test"]["context_block_count"], 1)
        self.assertFalse(replay["provider_request_test"]["provider_or_model_called"])
        self.assertFalse(replay["provider_request_test"]["qq_message_sent"])
        self.assertFalse(replay["privacy"]["user_chat_text_in_query"])
        self.assertFalse(replay["privacy"]["song_mid_in_query"])

    def test_final_live_anysearch_acceptance_has_sourced_provider_context(self):
        fixture = json.loads(FINAL_ACCEPTANCE_PATH.read_text(encoding="utf-8"))
        retrieval = fixture["retrieval"]
        request = fixture["provider_request"]

        self.assertEqual(retrieval["helper_invocation_count"], 1)
        self.assertEqual(retrieval["normalized_result_count"], 5)
        self.assertEqual(len(retrieval["results"]), 5)
        self.assertEqual(retrieval["result_shape"]["live_attributes_read"], ["title", "url", "snippet"])
        self.assertIn("item.content", retrieval["result_shape"]["host_helper_contract"])
        self.assertFalse(retrieval["results"][0]["safe_allowlisted_url"])
        self.assertTrue(retrieval["results"][3]["identity_match"])
        self.assertIn("白名单", retrieval["results"][0]["reason"])
        self.assertEqual(request["class"], "ProviderRequest")
        self.assertEqual(request["context_block_count"], 1)
        self.assertTrue(request["injected_once"])
        self.assertTrue(request["contains_sourced_public_fact"])
        self.assertEqual(request["fact_fields"]["曲风"]["confidence"], "unknown")
        self.assertEqual(
            request["accepted_source_fields"][0]["source"],
            "https://m.baike.com/wikiid/7951543283447711619",
        )
        self.assertFalse(request["provider_or_model_called"])
        self.assertFalse(request["qq_message_sent"])
        self.assertFalse(fixture["privacy"]["full_lyrics_saved"])
        self.assertFalse(fixture["privacy"]["audio_downloaded"])

    def test_public_source_replay_accepts_work_theme_and_song_genre_with_evidence(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        facts = interpret_results(
            [fixture["source"]], fixture["identity"]
        )

        self.assertEqual(facts["歌词主题"]["value"], "对妈妈的深厚感情")
        self.assertEqual(facts["歌词主题"]["scope"], "work")
        self.assertEqual(facts["歌词主题"]["source"], fixture["source"]["url"])
        self.assertEqual(
            facts["歌词主题"]["evidence"], fixture["source"]["evidence_excerpt"]
        )
        self.assertEqual(facts["曲风"]["value"], "嘻哈说唱")
        self.assertEqual(facts["曲风"]["scope"], "work")
        self.assertEqual(facts["曲风"]["confidence"], "medium")
        self.assertEqual(facts["曲风"]["source"], fixture["source"]["url"])
        self.assertEqual(facts["曲风"]["evidence"], fixture["source"]["title"])
        self.assertEqual(facts["曲风"]["reason"], "")
        self.assertEqual(facts["歌曲介绍"]["confidence"], "unknown")
        self.assertEqual(
            facts["歌曲介绍"]["reason"], fixture["expected_fields"]["歌曲介绍"]["reason"]
        )
        self.assertFalse(fixture["privacy"]["lyrics_saved"])

    def test_normalized_snippet_and_raw_content_both_reach_identity_matcher(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        source = dict(fixture["source"])
        source["content"] = source.pop("snippet")

        facts = interpret_results([source], fixture["identity"])

        self.assertEqual(facts["歌词主题"]["value"], "对妈妈的深厚感情")
        self.assertEqual(facts["歌词主题"]["source"], fixture["source"]["url"])

    def test_song_genre_and_theme_are_work_level_without_album_match(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        identity = {**fixture["identity"], "album": "依然范特西"}
        facts = interpret_results([fixture["source"]], identity)

        self.assertEqual(facts["歌词主题"]["confidence"], "medium")
        self.assertEqual(facts["曲风"]["value"], "嘻哈说唱")
        self.assertEqual(facts["曲风"]["scope"], "work")
        self.assertEqual(facts["曲风"]["confidence"], "medium")
        self.assertEqual(facts["歌曲介绍"]["confidence"], "unknown")

    def test_public_baike_title_directly_labels_work_genre_without_card_album(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        facts = interpret_results([fixture["source"]], fixture["identity"])

        self.assertEqual(facts["曲风"]["value"], "嘻哈说唱")
        self.assertEqual(facts["曲风"]["scope"], "work")
        self.assertEqual(facts["曲风"]["source"], fixture["source"]["url"])
        self.assertEqual(facts["曲风"]["evidence"], fixture["source"]["title"])

    def test_live_anysearch_mood_words_are_not_a_named_genre(self):
        fixture = json.loads(FINAL_ACCEPTANCE_PATH.read_text(encoding="utf-8"))
        live_result = fixture["retrieval"]["results"][3]
        source = {
            "title": live_result["title"],
            "url": live_result["url"],
            "snippet": live_result["short_non_lyric_excerpt"],
        }

        facts = interpret_results([source], fixture["identity"])

        self.assertEqual(facts["曲风"]["confidence"], "unknown")
        self.assertIn("不把心情形容", facts["曲风"]["reason"])

    def test_album_overall_genre_is_not_inferred_as_song_genre(self):
        identity = {"title": "第一首", "artist": "歌手甲", "album": ""}
        for snippet in (
            "该专辑整体曲风为流行摇滚。",
            "专辑《专辑甲》的整体曲风为流行摇滚。",
        ):
            with self.subTest(snippet=snippet):
                source = {
                    "title": "第一首 - 歌手甲",
                    "url": "https://baike.baidu.com/item/example",
                    "snippet": snippet,
                }

                facts = interpret_results([source], identity)

                self.assertEqual(facts["曲风"]["confidence"], "unknown")
                self.assertEqual(facts["曲风"]["source"], "")

    def test_explicit_song_level_genre_prose_is_accepted_without_album(self):
        identity = {"title": "第一首", "artist": "歌手甲", "album": ""}
        source = {
            "title": "第一首 - 歌手甲",
            "url": "https://baike.baidu.com/item/example",
            "snippet": "《第一首》的曲风为流行摇滚。",
        }

        facts = interpret_results([source], identity)

        self.assertEqual(facts["曲风"]["value"], "流行摇滚")
        self.assertEqual(facts["曲风"]["scope"], "work")
        self.assertEqual(facts["曲风"]["source"], source["url"])
        self.assertEqual(facts["曲风"]["evidence"], source["snippet"])

    def test_conflicting_live_recording_genre_title_is_rejected_without_album(self):
        identity = {"title": "第一首", "artist": "歌手甲", "album": ""}
        source = {
            "title": "第一首 Live版（歌手甲演唱）",
            "url": "https://baike.baidu.com/item/example",
            "snippet": "曲风为流行摇滚。",
        }

        facts = interpret_results([source], identity)

        self.assertEqual(facts["曲风"]["confidence"], "unknown")

    def test_song_genre_requires_matching_artist(self):
        identity = {"title": "第一首", "artist": "歌手甲", "album": ""}
        source = {
            "title": "第一首 - 歌手乙",
            "url": "https://baike.baidu.com/item/example",
            "snippet": "《第一首》的曲风为流行摇滚。",
        }

        facts = interpret_results([source], identity)

        self.assertEqual(facts["曲风"]["confidence"], "unknown")

    def test_live_word_in_body_does_not_reject_a_matching_recording(self):
        identity = {"title": "第一首", "artist": "歌手甲", "album": "专辑甲"}
        source = {
            "title": "第一首（歌手甲演唱）",
            "url": "https://baike.baidu.com/item/example",
            "snippet": "收录于专辑《专辑甲》，曲风为流行摇滚；正文还提到另一首歌的现场版。",
        }

        facts = interpret_results([source], identity)

        self.assertEqual(facts["曲风"]["value"], "流行摇滚")
        self.assertEqual(facts["曲风"]["confidence"], "medium")

    def test_title_adjacent_live_version_and_album_page_are_rejected(self):
        identity = {"title": "第一首", "artist": "歌手甲", "album": "专辑甲"}
        live_title = {
            "title": "第一首 Live版（歌手甲演唱）",
            "url": "https://baike.baidu.com/item/example",
            "snippet": "收录于专辑《专辑甲》，曲风为流行摇滚。",
        }
        album_page = {
            "title": "第一首 - 歌手甲",
            "url": "https://music.apple.com/cn/album/example/123",
            "snippet": "专辑曲目与主题信息。",
        }

        self.assertEqual(
            QQMusicPublicFacts.recording_source_candidate(live_title, identity), None
        )
        self.assertEqual(
            QQMusicPublicFacts.recording_source_candidate(album_page, identity), None
        )

    def test_wrong_album_rejects_recording_facts_but_not_work_theme(self):
        identity = {"title": "第一首", "artist": "歌手甲", "album": "专辑甲"}
        source = {
            "title": "第一首（歌手甲演唱）",
            "url": "https://baike.baidu.com/item/example",
            "snippet": "收录于专辑《现场精选》，歌词主题表达青春与回忆；正文还提到该歌手的现场版。",
        }

        facts = interpret_results([source], identity)

        self.assertEqual(facts["歌词主题"]["value"], "青春与回忆")
        self.assertEqual(facts["歌词主题"]["scope"], "work")
        self.assertEqual(facts["歌词主题"]["confidence"], "medium")
        self.assertEqual(facts["曲风"]["confidence"], "unknown")
        self.assertEqual(facts["歌曲介绍"]["confidence"], "unknown")
        self.assertIn("没有明确说明歌曲曲风", facts["曲风"]["reason"])

    def test_generic_version_word_alone_is_not_a_recording_conflict(self):
        identity = {"title": "第一首", "artist": "歌手甲", "album": "专辑甲"}
        source = {
            "title": "第一首（歌手甲演唱，版本介绍）",
            "url": "https://baike.baidu.com/item/example",
            "snippet": "收录于专辑《专辑甲》，曲风为流行摇滚。",
        }

        facts = interpret_results([source], identity)

        self.assertEqual(facts["曲风"]["value"], "流行摇滚")
        self.assertEqual(facts["曲风"]["confidence"], "medium")

    def test_script_variants_do_not_cross_match_without_normalization(self):
        identity = {"title": "听妈妈的话", "artist": "周杰伦", "album": ""}
        traditional_result = {
            "title": "聽媽媽的話 - 周杰伦",
            "url": "https://baike.baidu.com/item/example",
            "snippet": "歌曲主题表达对妈妈的深厚感情。",
        }

        facts = interpret_results([traditional_result], identity)

        self.assertEqual(facts["歌词主题"]["confidence"], "unknown")
        self.assertEqual(
            facts["歌词主题"]["reason"], "没有同时匹配歌名与歌手的作品级来源"
        )

    def test_verified_page_excerpt_retains_matched_identity_and_exact_source(self):
        identity = {"title": "听妈妈的话", "artist": "周杰伦", "album": ""}
        page = _VerifiedPageExcerpt(
            title="听妈妈的话 - 百科",
            url="https://www.baike.com/wikiid/7951543283447711619",
            snippet="字里行间表达了他对妈妈的深厚感情",
            matched_title=identity["title"],
            matched_artist=identity["artist"],
        )

        facts = interpret_results([page], identity)

        self.assertEqual(facts["歌词主题"]["value"], "对妈妈的深厚感情")
        self.assertEqual(facts["歌词主题"]["source"], page.url)
        self.assertEqual(facts["歌词主题"]["evidence"], page.snippet)

    def test_page_excerpt_requires_identity_and_keeps_one_short_clause(self):
        html = (
            "<html><head><title>听妈妈的话 - 百科</title></head><body>"
            "<p>周杰伦演唱的歌曲，字里行间表达了他对妈妈的深厚感情。</p>"
            "<p>歌词全文：此段不应保留。</p></body></html>"
        )

        excerpt = _page_evidence_excerpt(
            html,
            {"title": "听妈妈的话", "artist": "周杰伦"},
            "听妈妈的话 - 百科",
        )
        mismatched = _page_evidence_excerpt(
            html,
            {"title": "另一首歌", "artist": "周杰伦"},
            "听妈妈的话 - 百科",
        )

        self.assertEqual(excerpt, "字里行间表达了他对妈妈的深厚感情")
        self.assertEqual(mismatched, "")

    def test_song_and_source_urls_are_validated_without_network_access(self):
        self.assertEqual(
            parse_qq_song_mid("https://y.qq.com/n/ryqq/songDetail/0034MnYb0qxYhV"),
            "0034MnYb0qxYhV",
        )
        self.assertEqual(
            parse_qq_song_mid("https://y.qq.com.evil.example/n/ryqq/songDetail/0034MnYb0qxYhV"),
            "",
        )
        self.assertEqual(
            parse_qq_song_mid("http://y.qq.com/n/ryqq/songDetail/0034MnYb0qxYhV"),
            "",
        )
        self.assertEqual(
            safe_source_url("https://baike.baidu.com/item/example"),
            "https://baike.baidu.com/item/example",
        )
        self.assertEqual(
            safe_source_url("https://baike.baidu.com.evil.example/item/example"),
            "",
        )


class GenreAutomaticLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_identity_lookup_returns_baike_work_genre(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        resolver = QQMusicPublicFacts()
        with patch.object(
            resolver,
            "search_card_identity",
            AsyncMock(return_value=[fixture["source"]]),
        ) as search:
            identity, facts = await resolver.public_facts_for_card(fixture["identity"])

        search.assert_awaited_once()
        self.assertEqual(identity["title"], fixture["identity"]["title"])
        self.assertEqual(facts["曲风"]["value"], "嘻哈说唱")
        self.assertEqual(facts["曲风"]["scope"], "work")
        self.assertEqual(facts["曲风"]["source"], fixture["source"]["url"])


class ContextBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_payload_is_bounded_and_deduplicated(self):
        blocks = normalize_context_blocks(["same", "same", "", None, "x" * 5000])

        self.assertEqual(blocks[0], "same")
        self.assertEqual(len(blocks), 2)
        self.assertEqual(len(blocks[1]), 1200)
        self.assertLessEqual(sum(map(len, blocks)), 3600)

    async def test_card_lookup_task_survives_a_debounce_batch_once(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        payload = make_card_payload()
        card_event = Event(
            message=[Json(payload), Json(payload)],
            raw_message={"message": [{"type": "json", "data": payload}]},
        )
        self.assertEqual(len(extract_qq_music_cards(card_event)), 1)

        music = object.__new__(QQMusicContextPlugin)
        music.enabled = True
        music.resolver = QQMusicPublicFacts()
        music._pending_tasks = set()
        search = AsyncMock(return_value=[fixture["source"]])

        merged_event = Event()
        merged_event.media = []
        debounce = object.__new__(ContinuousMessagePlugin)
        debounce.merge_separator = "\n"
        debounce.context = None
        debounce.link_parser = SimpleNamespace(
            enrich=AsyncMock(return_value=(
                "QQ music card: 听妈妈的话 / 周杰伦\nFOLLOW_UP_QUESTION",
                ["image://cover"],
            ))
        )
        debounce.image_localizer = SimpleNamespace(
            enabled=False,
            localize=AsyncMock(return_value=["image://cover"]),
        )
        debounce._caption_private_images = AsyncMock(return_value="")

        def reconstruct(event, text, images, **_kwargs):
            event.message_str = text
            event.media = images

        debounce.parser = SimpleNamespace(reconstruct_event=reconstruct)
        with patch(
            "astrbot.core.tools.web_search_tools._anysearch_search", new=search
        ):
            await music.capture_music_card_context(card_event)
            context_task = card_event.get_extra(EVENT_CONTEXT_EXTRA_KEY)
            self.assertTrue(asyncio.isfuture(context_task))
            await debounce._finalize_merged(
                merged_event,
                ["QQ music card: 听妈妈的话 / 周杰伦", "FOLLOW_UP_QUESTION"],
                ["image://cover"],
                [context_task, context_task],
            )

        request = SimpleNamespace(prompt=merged_event.message_str, system_prompt="")
        await music.inject_music_context(merged_event, request)
        await debounce.inject_event_context(merged_event, request)

        self.assertIn("QQ music card: 听妈妈的话 / 周杰伦", request.prompt)
        self.assertIn("FOLLOW_UP_QUESTION", request.prompt)
        self.assertEqual(request.prompt.count("字里行间表达了他对妈妈的深厚感情"), 1)
        self.assertIn(fixture["source"]["url"], request.prompt)
        self.assertIn("unknown（未找到可核验的公开字段）", request.prompt)
        self.assertIn('"confidence":"medium"', request.prompt)
        self.assertEqual(merged_event.media, ["image://cover"])
        self.assertTrue(merged_event.get_extra(EVENT_CONTEXT_APPLIED_EXTRA_KEY))
        self.assertTrue(merged_event.get_extra(EVENT_CONTEXT_INJECTED_EXTRA_KEY))
        search.assert_awaited_once()
        settings, search_request = search.await_args.args
        self.assertEqual(settings, {"websearch_anysearch_key": []})
        self.assertEqual(
            search_request["query"],
            '"听妈妈的话" "周杰伦" 曲风 歌曲介绍 歌词主题 创作背景',
        )
        debounce.link_parser.enrich.assert_awaited_once()

    async def test_disabling_music_context_does_not_change_debounce_merge(self):
        payload = make_card_payload()
        event = Event(
            message=[Json(payload)],
            raw_message={"message": [{"type": "json", "data": payload}]},
        )
        music = object.__new__(QQMusicContextPlugin)
        music.enabled = False
        music.resolver = SimpleNamespace(public_facts_for_card=AsyncMock())
        music._pending_tasks = set()

        await music.capture_music_card_context(event)

        self.assertIsNone(event.get_extra(EVENT_CONTEXT_EXTRA_KEY))
        music.resolver.public_facts_for_card.assert_not_awaited()

        merged_event = Event()
        merged_event.media = []
        debounce = object.__new__(ContinuousMessagePlugin)
        debounce.merge_separator = "\n"
        debounce.link_parser = SimpleNamespace(
            enrich=AsyncMock(return_value=("普通消息一\n普通消息二", []))
        )
        debounce.image_localizer = SimpleNamespace(
            enabled=False,
            localize=AsyncMock(return_value=[]),
        )
        debounce._caption_private_images = AsyncMock(return_value="")

        def reconstruct(target, text, images, **_kwargs):
            target.message_str = text
            target.media = images

        debounce.parser = SimpleNamespace(reconstruct_event=reconstruct)
        await debounce._finalize_merged(
            merged_event,
            ["普通消息一", "普通消息二"],
            [],
            [],
        )

        self.assertEqual(merged_event.message_str, "普通消息一\n普通消息二")
        debounce.link_parser.enrich.assert_awaited_once()

        disabled_request = ProviderRequest(prompt="原问题")
        disabled_event = Event()
        disabled_event.set_extra(EVENT_CONTEXT_EXTRA_KEY, ["should not append"])
        await music.inject_music_context(disabled_event, disabled_request)
        self.assertEqual(disabled_request.prompt, "原问题")

    async def test_3_5_second_card_and_text_batch_searches_and_injects_once(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        payload = make_card_payload()
        card_event = Event(
            message=[Json(payload)],
            raw_message={"message": [{"type": "json", "data": payload}]},
        )
        card_event.unified_msg_origin = "test:friend:3_5_second_batch"
        card_event.message_obj.message_id = "card-1"
        card_event.message_obj.test_text = "QQ music card: 听妈妈的话 / 周杰伦"
        follow_up_event = Event()
        follow_up_event.unified_msg_origin = card_event.unified_msg_origin
        follow_up_event.message_obj.message_id = "text-1"
        follow_up_event.message_obj.test_text = "FOLLOW_UP_QUESTION"
        follow_up_event.message_str = "FOLLOW_UP_QUESTION"

        music = object.__new__(QQMusicContextPlugin)
        music.enabled = True
        music.resolver = QQMusicPublicFacts()
        music._pending_tasks = set()
        search = AsyncMock(return_value=[fixture["source"]])

        debounce = object.__new__(ContinuousMessagePlugin)
        debounce.enable_plugin = True
        debounce.debounce_time = 3.5
        debounce.debounce_time_by_umo = {}
        debounce.enable_typing_detection = False
        debounce.enable_recall_filter = False
        debounce.enable_forward_analysis = False
        debounce.command_prefixes = []
        debounce.merge_separator = "\n"
        debounce.adaptive_short_message_threshold = 80
        debounce.sessions = {}
        debounce._is_private_message_event = lambda _event: True
        debounce.access_control = SimpleNamespace(get_mode=lambda _event: "allow")
        debounce._calculate_wait_duration = lambda *_args, **_kwargs: (3.5, False, "fixed")
        debounce._format_wait_reason = lambda reason: reason
        debounce._silence_event = lambda event: setattr(event, "silenced", True)

        def parse_message(message_obj, *, allow_qq_music):
            self.assertFalse(allow_qq_music)
            return message_obj.test_text, False, []

        def get_message_id(event):
            return event.message_obj.message_id

        def reconstruct(event, text, images, **_kwargs):
            event.message_str = text
            event.media = images

        debounce.parser = SimpleNamespace(
            parse_message=parse_message,
            is_command=lambda _text, _prefixes: False,
            get_message_id=get_message_id,
            reconstruct_event=reconstruct,
        )
        debounce.link_parser = SimpleNamespace(
            enrich=AsyncMock(side_effect=lambda text, images, **_kwargs: (text, images))
        )
        debounce.image_localizer = SimpleNamespace(
            enabled=False,
            localize=AsyncMock(side_effect=lambda images: images),
        )
        debounce._caption_private_images = AsyncMock(return_value="")

        first_pipeline = None
        with patch(
            "astrbot.core.tools.web_search_tools._anysearch_search", new=search
        ):
            await music.capture_music_card_context(card_event)
            first_pipeline = asyncio.create_task(
                debounce.handle_private_msg(card_event)
            )
            try:
                await asyncio.sleep(0.05)
                await debounce.handle_private_msg(follow_up_event)
                await asyncio.wait_for(first_pipeline, timeout=4.5)
            finally:
                if not first_pipeline.done():
                    first_pipeline.cancel()
                    await asyncio.gather(first_pipeline, return_exceptions=True)

        request = ProviderRequest(prompt=card_event.message_str)
        await music.inject_music_context(card_event, request)
        await debounce.inject_event_context(card_event, request)

        self.assertIsInstance(request, ProviderRequest)
        self.assertIn(fixture["source"]["url"], request.prompt)
        self.assertIn(fixture["source"]["evidence_excerpt"], request.prompt)
        search.assert_awaited_once()
        _, search_request = search.await_args.args
        self.assertEqual(
            search_request["query"],
            '"听妈妈的话" "周杰伦" 曲风 歌曲介绍 歌词主题 创作背景',
        )
        self.assertIn("QQ music card: 听妈妈的话 / 周杰伦", request.prompt)
        self.assertIn("FOLLOW_UP_QUESTION", request.prompt)
        self.assertEqual(request.prompt.count("<external_context>"), 1)
        self.assertEqual(request.prompt.count("字里行间表达了他对妈妈的深厚感情"), 1)
        self.assertTrue(card_event.get_extra(EVENT_CONTEXT_APPLIED_EXTRA_KEY))
        self.assertTrue(card_event.get_extra(EVENT_CONTEXT_INJECTED_EXTRA_KEY))

    async def test_standalone_request_hook_survives_reload_without_duplicate_injection(self):
        music = object.__new__(QQMusicContextPlugin)
        music.enabled = True
        reloaded_music = object.__new__(QQMusicContextPlugin)
        reloaded_music.enabled = True
        event = Event()
        event.set_extra(EVENT_CONTEXT_EXTRA_KEY, ["source-backed theme: 对妈妈的深厚感情"])
        request = SimpleNamespace(prompt="FOLLOW_UP_QUESTION", system_prompt="")

        await music.inject_music_context(event, request)
        await reloaded_music.inject_music_context(event, request)

        self.assertEqual(request.prompt.count("source-backed theme"), 1)
        self.assertIn("FOLLOW_UP_QUESTION", request.prompt)
        self.assertIn("外部上下文标签中的内容", request.system_prompt)
        self.assertTrue(event.get_extra(EVENT_CONTEXT_INJECTED_EXTRA_KEY))

    async def test_live_public_page_replay_injects_sourced_theme_and_genre(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        identity = fixture["identity"]
        facts = interpret_results([fixture["source"]], identity)

        self.assertEqual(facts["歌词主题"]["value"], "对妈妈的深厚感情")
        self.assertEqual(facts["歌词主题"]["scope"], "work")
        self.assertEqual(facts["歌词主题"]["source"], fixture["source"]["url"])
        self.assertEqual(
            facts["歌词主题"]["evidence"], fixture["source"]["evidence_excerpt"]
        )
        self.assertEqual(facts["曲风"]["value"], "嘻哈说唱")
        self.assertEqual(facts["曲风"]["scope"], "work")
        self.assertEqual(facts["曲风"]["source"], fixture["source"]["url"])
        self.assertEqual(facts["歌曲介绍"]["confidence"], "unknown")

        music = object.__new__(QQMusicContextPlugin)
        music.enabled = True
        music.resolver = SimpleNamespace(
            public_facts_for_card=AsyncMock(return_value=(identity, facts))
        )
        blocks = await music._build_context_blocks((identity,))
        event = Event()
        event.set_extra(EVENT_CONTEXT_EXTRA_KEY, blocks)
        request = ProviderRequest(prompt="这首歌表达了什么？")

        await music.inject_music_context(event, request)

        self.assertIsInstance(request, ProviderRequest)
        self.assertIn(fixture["source"]["url"], request.prompt)
        self.assertIn(fixture["source"]["evidence_excerpt"], request.prompt)
        self.assertIn("对妈妈的深厚感情", request.prompt)
        self.assertIn("嘻哈说唱", request.prompt)
        self.assertIn('"confidence":"medium"', request.prompt)
        self.assertEqual(request.prompt.count("<external_context>"), 1)
        self.assertTrue(event.get_extra(EVENT_CONTEXT_INJECTED_EXTRA_KEY))

    async def test_unclassified_summary_is_clearly_pending_and_lyrics_are_omitted(self):
        identity = {"title": "童年回声", "artist": "歌手甲", "album": "", "mid": ""}
        source = {
            "title": "童年回声 - 歌手甲",
            "url": "https://baike.baidu.com/item/example",
            "snippet": "歌曲以童年回忆和家庭叙事串起成长经历。",
        }
        facts = interpret_results([source], identity)

        self.assertEqual(facts["歌词主题"]["confidence"], "unknown")
        pending = facts["待模型分析的来源摘要"]
        self.assertEqual(pending["confidence"], "unclassified")
        self.assertEqual(pending["source"], source["url"])
        self.assertEqual(pending["evidence"], source["snippet"])
        self.assertIn("不代表已确认", pending["reason"])

        music = object.__new__(QQMusicContextPlugin)
        music.enabled = True
        music.resolver = SimpleNamespace(
            public_facts_for_card=AsyncMock(return_value=(identity, facts))
        )
        blocks = await music._build_context_blocks((identity,))
        event = Event()
        event.set_extra(EVENT_CONTEXT_EXTRA_KEY, blocks)
        request = ProviderRequest(prompt="你如何理解这首歌？")
        await music.inject_music_context(event, request)

        self.assertIn(source["url"], request.prompt)
        self.assertIn(source["snippet"], request.prompt)
        self.assertIn('"confidence":"unclassified"', request.prompt)
        self.assertEqual(facts["歌词主题"]["value"], "unknown（未找到可核验的公开字段）")

        lyric_source = {**source, "snippet": "歌词片段：样例内容已省略，不存储歌词。"}
        lyric_facts = interpret_results([lyric_source], identity)
        self.assertNotIn("待模型分析的来源摘要", lyric_facts)

    async def test_debounce_off_uses_one_existing_provider_request(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        payload = make_card_payload()
        event = Event(
            message=[Json(payload)],
            raw_message={"message": [{"type": "json", "data": payload}]},
        )
        music = object.__new__(QQMusicContextPlugin)
        music.enabled = True
        music.resolver = QQMusicPublicFacts()
        music._pending_tasks = set()
        search = AsyncMock(return_value=[fixture["source"]])
        request = ProviderRequest(prompt="这首歌的主题是什么？")

        with patch("astrbot.core.tools.web_search_tools._anysearch_search", new=search):
            await music.capture_music_card_context(event)
            await music.inject_music_context(event, request)

        self.assertIsInstance(request, ProviderRequest)
        self.assertIn("这首歌的主题是什么？", request.prompt)
        self.assertIn("对妈妈的深厚感情", request.prompt)
        self.assertEqual(request.prompt.count("<external_context>"), 1)
        search.assert_awaited_once()

    async def test_timeout_skips_unresolved_context_without_blocking_request(self):
        async def wait_forever():
            await asyncio.Event().wait()

        event = Event()
        context_task = asyncio.create_task(wait_forever())
        event.set_extra(EVENT_CONTEXT_EXTRA_KEY, context_task)
        music = object.__new__(QQMusicContextPlugin)
        music.enabled = True
        request = ProviderRequest(prompt="继续回答")

        with patch("astrbot_plugin_qq_music_context.main.CONTEXT_WAIT_SECONDS", 0.001):
            await music.inject_music_context(event, request)

        self.assertEqual(request.prompt, "继续回答")
        self.assertFalse(event.get_extra(EVENT_CONTEXT_INJECTED_EXTRA_KEY, False))
        self.assertFalse(context_task.done())
        context_task.cancel()
        await asyncio.gather(context_task, return_exceptions=True)

    async def test_plugin_termination_cancels_background_lookup(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked_search(_settings, _request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        payload = make_card_payload()
        event = Event(
            message=[Json(payload)],
            raw_message={"message": [{"type": "json", "data": payload}]},
        )
        music = object.__new__(QQMusicContextPlugin)
        music.enabled = True
        music.resolver = QQMusicPublicFacts()
        music._pending_tasks = set()

        with patch(
            "astrbot.core.tools.web_search_tools._anysearch_search",
            new=AsyncMock(side_effect=blocked_search),
        ):
            await music.capture_music_card_context(event)
            await asyncio.wait_for(started.wait(), timeout=1)
            await music.terminate()

        self.assertTrue(cancelled.is_set())
        self.assertFalse(music._pending_tasks)
        self.assertFalse(music.resolver._pending_lookups)

    async def test_two_distinct_cards_in_one_batch_add_two_contexts_once(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        second_payload = {
            "app": "com.tencent.structmsg",
            "meta": {
                "music": {
                    "tag": "QQ音乐",
                    "title": "彩虹",
                    "singer": "周杰伦",
                    "jumpUrl": "https://y.qq.com/n/ryqq/songDetail/0034MnYb0qxYhW",
                }
            },
        }
        first_event = Event(
            message=[Json(make_card_payload())],
            raw_message={"message": [{"type": "json", "data": make_card_payload()}]},
        )
        second_event = Event(
            message=[Json(second_payload)],
            raw_message={"message": [{"type": "json", "data": second_payload}]},
        )
        music = object.__new__(QQMusicContextPlugin)
        music.enabled = True
        music.resolver = QQMusicPublicFacts()
        music._pending_tasks = set()

        async def public_search(_settings, request):
            if '"听妈妈的话"' in request["query"]:
                return [fixture["source"]]
            return [
                {
                    "title": "彩虹",
                    "url": "https://wapbaike.baidu.com/item/example",
                    "snippet": "周杰伦演唱；歌曲主题表达爱情中的希望。",
                }
            ]

        search = AsyncMock(side_effect=public_search)
        merged_event = Event()
        debounce = object.__new__(ContinuousMessagePlugin)
        debounce.merge_separator = "\n"
        debounce.link_parser = SimpleNamespace(
            enrich=AsyncMock(return_value=("卡片一\n卡片二\n问题", []))
        )
        debounce.image_localizer = SimpleNamespace(
            enabled=False,
            localize=AsyncMock(return_value=[]),
        )
        debounce._caption_private_images = AsyncMock(return_value="")
        debounce.parser = SimpleNamespace(
            reconstruct_event=lambda event, text, images, **_kwargs: setattr(
                event, "message_str", text
            )
        )

        with patch(
            "astrbot.core.tools.web_search_tools._anysearch_search", new=search
        ):
            await music.capture_music_card_context(first_event)
            await music.capture_music_card_context(second_event)
            await debounce._finalize_merged(
                merged_event,
                ["卡片一", "卡片二", "问题"],
                [],
                [
                    first_event.get_extra(EVENT_CONTEXT_EXTRA_KEY),
                    second_event.get_extra(EVENT_CONTEXT_EXTRA_KEY),
                ],
            )

        request = ProviderRequest(prompt=merged_event.message_str)
        await music.inject_music_context(merged_event, request)
        await debounce.inject_event_context(merged_event, request)

        self.assertEqual(search.await_count, 2)
        self.assertEqual(request.prompt.count("<external_context>"), 1)
        self.assertIn('"歌名":"听妈妈的话"', request.prompt)
        self.assertIn('"歌名":"彩虹"', request.prompt)
        self.assertIn("对妈妈的深厚感情", request.prompt)
        self.assertIn("爱情中的希望", request.prompt)

    async def test_group_events_do_not_start_search_or_attach_context(self):
        payload = make_card_payload()
        event = Event(
            message=[Json(payload)],
            message_type=MessageType.GROUP_MESSAGE,
        )
        music = object.__new__(QQMusicContextPlugin)
        music.enabled = True
        music._pending_tasks = set()
        music.resolver = SimpleNamespace(public_facts_for_card=AsyncMock())

        await music.capture_music_card_context(event)

        self.assertIsNone(event.get_extra(EVENT_CONTEXT_EXTRA_KEY))
        music.resolver.public_facts_for_card.assert_not_awaited()

    async def test_search_payload_contains_identity_and_fixed_intents_not_chat_text(self):
        parser = QQMusicPublicFacts()
        search = AsyncMock(return_value=[])
        with patch(
            "astrbot.core.tools.web_search_tools._anysearch_search", new=search
        ):
            await parser.search_card_identity(
                {
                    "title": "听妈妈的话",
                    "artist": "周杰伦",
                    "album": "依然范特西",
                    "mid": "0034MnYb0qxYhV",
                    "chat_text": "follow_up_text_must_not_be_sent",
                }
            )

        settings, request = search.await_args.args
        self.assertEqual(settings, {"websearch_anysearch_key": []})
        self.assertEqual(
            request["query"],
            '"听妈妈的话" "周杰伦" "依然范特西" 曲风 歌曲介绍 歌词主题 创作背景',
        )
        self.assertNotIn("0034MnYb0qxYhV", request["query"])
        self.assertNotIn("follow_up_text_must_not_be_sent", request["query"])
