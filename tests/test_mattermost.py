import asyncio
import json
import random
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from mm_fixture import MattermostFixture

from aelix_mattermost.config import Config
from aelix_mattermost.mattermost import (
    AuthenticationError, ForbiddenError, MattermostClient, MattermostError, neutralize_mentions,
    split_message,
)
from aelix_mattermost.mentions import text_ranges
from aelix_mattermost.storage import Store

FIXTURE = Path(__file__).with_name("mm_fixture.py").resolve()
W = "\u2060"
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
# Spelled out, not imported: no link previews, and the BOT badge survives a patch (F3).
PROPS = {"unsafe_links": "true", "from_bot": "true"}


def event(post_id="p1", user="u1", channel="c1", kind="D", text="hello", root=""):
    return {"event": "posted", "data": {"channel_type": kind, "post": json.dumps({
        "id": post_id, "user_id": user, "channel_id": channel, "message": text,
        "root_id": root, "type": "", "delete_at": 0,
    })}}


class NeutralizeMentionsTests(unittest.TestCase):
    def test_mentions_the_server_would_notify_are_neutralized(self):
        cases = {
            "@channel": f"@{W}channel",
            "hi @all and @HERE": f"hi @{W}all and @{W}HERE",
            "@user1, you can use @systembot": f"@{W}user1, you can use @{W}systembot",
            "this is a message for @user.name.": f"this is a message for @{W}user.name.",
            "(@here) @channel: @all-": f"(@{W}here) @{W}channel: @{W}all-",
            "@사용자 안녕": f"@{W}사용자 안녕",
            "*@aaa* **@bbb** ~~@ccc~~": f"*@{W}aaa* **@{W}bbb** ~~@{W}ccc~~",
            "### @aaa\n> @bbb\n- @ccc\n1. @ddd": f"### @{W}aaa\n> @{W}bbb\n- @{W}ccc\n1. @{W}ddd",
            "`this should mention @channel``": f"`this should mention @{W}channel``",
            "[@bob](https://example.com)": f"[@{W}bob](https://example.com)",
            "para\n    @lazy": f"para\n    @{W}lazy",
            ":smile:@channel": f":smile:@{W}channel",
            "\\@channel \\`@here`": f"\\@{W}channel \\`@{W}here`",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(neutralize_mentions(text), expected)

    def test_words_the_server_splits_or_trims(self):
        # server/channels/app/mention_parser_standard.go trims ":.-_" and re-splits on ".-:".
        for prefix in (".", ":", "-", "_", "user.", "user-", "user:", "a_.", "(_"):
            with self.subTest(prefix=prefix):
                self.assertEqual(neutralize_mentions(prefix + "@channel"), f"{prefix}@{W}channel")

    def test_entities_that_decode_to_mentions(self):
        self.assertEqual(neutralize_mentions("&#64;channel"), f"&#64;{W}channel")
        self.assertEqual(neutralize_mentions("&#x40;here &commat;all"), f"&#x40;{W}here &commat;{W}all")
        self.assertEqual(neutralize_mentions("@&#99;hannel"), f"@{W}&#99;hannel")

    def test_non_mentions_and_code_are_untouched(self):
        for text in (
            "mail me@example.com", "user@aelix", "@@channel", "foo_@channel", "x:_@channel", "a1@b",
            "@ channel", "@", "@!",
            "`@channel`", "``@channel ` x``", "```\n@channel\n```", "~~~\n@here\n~~~", "    @channel",
            "para\n\n    @Override\n    def f(): pass", "```python\n@decorator\ndef f(): ...\n```",
            "- ```\n  @x\n  ```", "> ```\n> @x\n> ```",
            '[foo](this "shouldn\'t mention @channel at all")', "[me](https://mastodon.social/@user)",
            # Bare URLs are autolinks, which never mention anyone; a joiner would break them.
            "see https://www.npmjs.com/package/@types/node", "https://mastodon.social/@user",
            "www.npmjs.com/package/@scope/pkg", "(https://x.com/@all)",
            '![a](x.png =10x20 "@here")', f"@{W}channel", "plain text without mentions",
        ):
            with self.subTest(text=text):
                self.assertEqual(neutralize_mentions(text), text)

    def test_markdown_structure_follows_the_server_parser(self):
        cases = {
            # The fence lives in the list item / quote, so the text after it is a paragraph.
            "- ```\n  @x\n  ```\n@channel": f"- ```\n  @x\n  ```\n@{W}channel",
            "> ```\n> @x\nplain @channel": f"> ```\n> @x\nplain @{W}channel",
            # Mattermost lets an indented fence interrupt a paragraph.
            "para\n    ```\n@channel\n    ```": "para\n    ```\n@channel\n    ```",
            # An autolink swallows backticks, and the server never reads autolink text.
            "https://x.com/`@channel`": "https://x.com/`@channel`",
            "https://x.com/a. @channel `@here`": f"https://x.com/a. @{W}channel `@here`",
            "`a`@channel": f"`a`@{W}channel",
            # ":https:" is an emoji, so no autolink forms and the backticks are code.
            ":https://x.y/``@channel``": ":https://x.y/``@channel``",
            "a :https://x.y/@here": f"a :https://x.y/@{W}here",
            # An emoji ends the server's word buffer; "-_@x" then trims to "@x".
            ":e:-_@x": f":e:-_@{W}x",
            # A joiner after the first "@" starts a new word "_@user".
            "@_@user": f"@{W}_@{W}user",
            # "@" right before an autolink is a word of its own; a joiner would also
            # stop the server from linking "www." at the start of a paragraph.
            "@www.example.com is our site": "@www.example.com is our site",
            "@www.`1. @here`": f"@www.`1. @{W}here`",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(neutralize_mentions(text), expected)

    def test_link_text_splits_words_where_the_server_does(self):
        # The server inspects link and image text node by node, and "w", "W" and ":" are
        # nodes of their own there: "_@here" after one trims to "@here".
        cases = {
            "[w_@here](u)": f"[w_@{W}here](u)", "[aw@x](u)": f"[aw@{W}x](u)", "[a:_@all](u)": f"[a:_@{W}all](u)",
            "![a:-_@x](u)": f"![a:-_@{W}x](u)", "[a:_@x][r]\n\n[r]: /u": f"[a:_@{W}x][r]\n\n[r]: /u",
            # Elsewhere the nodes are merged again, so these are single words without a mention.
            "aw@x a:_@x [a](u) w_@here": "aw@x a:_@x [a](u) w_@here",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(neutralize_mentions(text), expected)

    def test_reference_definitions_change_nothing_else(self):
        # Citation lists are common in model output; the server never reads definitions as text.
        for text in (
            "Use `@dataclass`.\n\n[1]: https://docs.python.org/3/library/dataclasses.html",
            "Profile: [me](https://mastodon.social/@user), post: https://medium.com/@author/p\n\n"
            "[1]: https://example.com",
            "`npm i @types/node` [1]\n\n[1]: https://www.npmjs.com/package/@types/node",
            '[me]: https://mastodon.social/@user "title @here"\n\nok', "[ref]: https://example.com\n\n`@channel`",
            "[x][@here]\n\n[@here]: https://x",  # a full reference consumes its label
            "[x][A` b] @here `c`",  # undefined: the backtick opens a code span
        ):
            with self.subTest(text=text):
                self.assertEqual(neutralize_mentions(text), text)

    def test_reference_links_follow_the_server(self):
        cases = {
            # A shortcut reference's label is visible text; an undefined one is plain text.
            "[@here][1]\n\n[1]: https://x": f"[@{W}here][1]\n\n[1]: https://x",
            "[x][@here]": f"[x][@{W}here]",
            # A consumed label hides its backtick, so "@here" is text and "`c`" is code.
            "[a`b]: /x\n\n[x][a`b] @here `c`": f"[a`b]: /x\n\n[x][a`b] @{W}here `c`",
            "[a`  B]: /x\n\n[x][A` b] @here `c`": f"[a`  B]: /x\n\n[x][A` b] @{W}here `c`",
            # A reference link deactivates the "[" before it: "(/u/@user)" is text.
            "[a [b][1] c](/u/@user)\n\n[1]: https://x": f"[a [b][1] c](/u/@{W}user)\n\n[1]: https://x",
            "[가]: /k\n\n[1] `@x` @y": f"[가]: /k\n\n[1] `@x` @{W}y",
            # Unlinking "[@here]" reactivates "[o", which turns the URL after it into text.
            "[o `x` [@here] https://x.y/@u\n\n[@here]: /h": f"[o `x` [@{W}here] https://x.y/@{W}u\n\n[@here]: /h",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(neutralize_mentions(text), expected)

    def test_vertical_tabs_and_form_feeds_become_spaces(self):
        # Where one starts a paragraph, the server's trimLeftSpace cuts the line short at its
        # end, which joins "\v@" to the next line: they become spaces first, in code too.
        cases = {
            "\v@\nchannel": " @\nchannel", "\f@\nall": " @\nall", "x\v :+1:._@all": f"x  :+1:._@{W}all",
            "```\n@x\n```\n\v`@y`": "```\n@x\n```\n `@y`", "```\na\fb\n```": "```\na b\n```",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(neutralize_mentions(text), expected)
        # text_ranges itself (routing) still reads every paragraph as text, code blocks not.
        text = "a\f\n\n`@y` [l](/u/@z)\n\n    @code"
        self.assertEqual([text[a:b] for a, b in text_ranges(text)], ["a", "`@y` [l](/u/@z)"])

    def test_unported_markdown_falls_back_to_plain_text(self):
        # Whether "ǆ" matches "ǅ" needs Unicode's case-folding tables, so that paragraph is
        # plain text. The server still parses its emoji, which split words, so every "@" gets
        # a joiner there, even in an email address.
        cases = {
            "[ǅ]: /x\n\n[ǆ] `@here`\n\n`@all`": f"[ǅ]: /x\n\n[ǆ] `@{W}here`\n\n`@all`",
            "[ǅ]: /x\n\n[ǆ] :e:._@here": f"[ǅ]: /x\n\n[ǆ] :e:._@{W}here",
            "[é]: /x\n\n[ü] :+1:-_@all me@x.org": f"[é]: /x\n\n[ü] :+1:-_@{W}all me@{W}x.org",
            # Hangul has no case: "[나]" certainly does not match "[가]", so nothing falls back.
            "[가]: /x\n\n[나] `@here`": "[가]: /x\n\n[나] `@here`",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(neutralize_mentions(text), expected)

    def test_reference_links_that_keep_changing_fall_back_to_plain_text(self):
        # Each round unlinks one more label ("[@a]", then "[@b]"...); after eight rounds every
        # paragraph is plain text, where every "@" gets a joiner, even in an email address.
        def cascade(levels):
            labels = [f"@{chr(97 + index)}" for index in range(levels)]
            return (f"[x [{labels[0]}] " + " ".join(f"https://h/[{label}]" for label in labels[1:])
                    + "\n\nme@x.org\n\n" + "\n".join(f"[{label}]: /{label[1:]}" for label in labels))

        self.assertIn("\n\nme@x.org\n\n", neutralize_mentions(cascade(3)))
        self.assertIn(f"\n\nme@{W}x.org\n\n", neutralize_mentions(cascade(12)))

    def test_idempotent(self):
        text = "@channel `@code` me@example.com &#64;here"
        once = neutralize_mentions(text)
        self.assertEqual(neutralize_mentions(once), once)


def is_fence(line):
    return FENCE.fullmatch(line) is not None


def balanced(chunk):
    opening = None
    for line in chunk.split("\n"):
        match = FENCE.fullmatch(line)
        if match is None:
            continue
        if opening is None:
            if match[1][0] == "~" or "`" not in match[2]:
                opening = match
        elif match[1][0] == opening[1][0] and len(match[1]) >= len(opening[1]) and not match[2].strip():
            opening = None
    return opening is None


class SplitMessageTests(unittest.TestCase):
    def test_cut_fence_is_closed_and_reopened_with_its_info_string(self):
        text = "intro\n```python\n" + "".join(f"print({i})\n" for i in range(60)) + "```\nafter"
        chunks = split_message(text, 150)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 150 for chunk in chunks))
        self.assertTrue(all(balanced(chunk) for chunk in chunks))
        for chunk in chunks[1:-1]:
            self.assertTrue(chunk.startswith("```python\n"))
            self.assertTrue(chunk.endswith("\n```"))
        self.assertTrue(chunks[-1].endswith("```\nafter"))
        lines = [x for chunk in chunks for x in chunk.split("\n") if x and not is_fence(x)]
        self.assertEqual(lines, [x for x in text.split("\n") if x and not is_fence(x)])

    def test_tilde_and_long_fences_and_text_without_fences(self):
        for opening, closing in (("~~~", "~~~"), ("````md", "````"), ("  ```", "```")):
            text = f"{opening}\n" + "x = 1\n" * 50 + closing + "\nend"
            chunks = split_message(text, 100)
            with self.subTest(opening=opening):
                self.assertTrue(all(len(chunk) <= 100 and balanced(chunk) for chunk in chunks))
                self.assertTrue(chunks[1].startswith(opening + "\n"))
        plain = "word " * 500
        self.assertEqual("".join(split_message(plain, 120)), plain)

    def test_hard_cut_never_splits_a_fence_line(self):
        text = "a" * 30 + "\n```" + "b" * 90 + "\ncode\n```\n"
        chunks = split_message(text, 100)
        self.assertEqual(chunks[0], "a" * 30 + "\n")
        self.assertTrue(chunks[1].startswith("```" + "b" * 90 + "\n"))
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))

    def test_random_documents_stay_within_limit_and_balanced(self):
        rng = random.Random(7)
        for _ in range(200):
            lines = []
            for _ in range(rng.randint(1, 60)):
                kind = rng.random()
                if kind < 0.1:
                    lines.append(rng.choice(["```", "```py", "~~~", "````", "~~~~ x"]))
                else:
                    lines.append("".join(rng.choice("ab@` ") for _ in range(rng.randint(1, 30))).strip() or "z")
            text = "\n".join(lines)
            limit = rng.randint(100, 300)
            chunks = split_message(text, limit)
            self.assertTrue(all(len(chunk) <= limit for chunk in chunks), (text, limit))
            if balanced(text):
                self.assertTrue(all(balanced(chunk) for chunk in chunks), (text, limit))
            kept = [x for chunk in chunks for x in chunk.split("\n") if x and not is_fence(x)]
            self.assertEqual(kept, [x for x in text.split("\n") if x and not is_fence(x)])


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = MattermostFixture()
        await self.server.start()
        self.config = Config(url=self.server.url, token="test-secret", allowed_users=("u1",),
                             allow_insecure_http=True, max_post_chars=100).validate()
        self.client = await MattermostClient(self.config).__aenter__()
        self.client.reconnect_delay = 0.05
        self.readers: list[asyncio.Task] = []

    async def asyncTearDown(self):
        for task in self.readers:
            task.cancel()
        await asyncio.gather(*self.readers, return_exceptions=True)
        await self.client.__aexit__()
        await self.server.close()

    def consume(self):
        queue: asyncio.Queue = asyncio.Queue()

        async def reader():
            async for packet in self.client.events():
                await queue.put(packet)

        task = asyncio.create_task(reader())
        self.readers.append(task)
        return queue, task

    async def until(self, condition, timeout=3.0):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("condition not reached")
            await asyncio.sleep(0.01)

    # -- REST --------------------------------------------------------------------

    async def test_post_and_patch_send_props_and_neutralize_outside_code(self):
        post = await self.client.post("c1", "root", "@channel see `@channel` and me@example.com")
        self.assertEqual(self.server.posts[0]["message"], f"@{W}channel see `@channel` and me@example.com")
        self.assertEqual(self.server.posts[0]["props"], PROPS)
        self.assertRegex(self.server.posts[0]["pending_post_id"], r"^[0-9a-f]{32}$")
        await self.client.patch(post["id"], "done @here\n```\n@here\n```")
        self.assertEqual(self.server.patches[0]["message"], f"done @{W}here\n```\n@here\n```")
        self.assertEqual(self.server.patches[0]["props"], PROPS)
        # A patch replaces every prop, so the BOT marker survives only because it is resent.
        self.assertEqual(self.server.stored[post["id"]]["props"], PROPS)

    async def test_reply_patches_placeholder_then_posts_remaining_chunks(self):
        placeholder = await self.client.post("c1", "root", "응답을 준비하고 있습니다…")
        await self.client.reply("c1", "root", "line @all\n" * 30, placeholder["id"])
        self.assertEqual(len(self.server.patches), 1)
        self.assertGreater(len(self.server.posts), 2)
        for value in self.server.patches + self.server.posts[1:]:
            self.assertEqual(value["props"], PROPS)
            self.assertNotIn("@all", value["message"])

    async def test_create_retries_reuse_pending_post_id_and_never_duplicate(self):
        self.server.fail("POST", "posts", 502, after=True)  # created, but the response is lost
        self.server.fail("POST", "posts", drop=True, after=True)
        post = await self.client.post("c1", "", "once")
        self.assertEqual(len(self.server.posts), 1)
        self.assertEqual(post["id"], self.server.posts[0]["id"])
        await self.client.post("c1", "", "twice")
        self.assertNotEqual(self.server.posts[0]["pending_post_id"], self.server.posts[1]["pending_post_id"])

    async def test_a_stalled_create_is_retried_within_the_dedup_window(self):
        # Created, but the response stalls (for 30 s or more on a loaded server).
        self.server.fail("POST", "posts", status=201, after=True, delay=3.0)
        started = time.monotonic()
        with patch("aelix_mattermost.mattermost.CREATE_TIMEOUT", 0.3, create=True):
            post = await self.client.post("c1", "", "once")
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual([x["id"] for x in self.server.posts], [post["id"]])  # the server's dedup answer

    async def test_create_is_not_retried_once_the_server_may_have_forgotten_it(self):
        # Scaled down from 30 s: the server forgets a pending_post_id after 0.4 s here, and
        # the response of a create it has already done is lost after 0.5 s.
        self.server.fail("POST", "posts", drop=True, after=True, delay=0.5)
        with patch("mm_fixture.DEDUP_SECONDS", 0.4), \
                patch("aelix_mattermost.mattermost.CREATE_RETRY_WINDOW", 0.3, create=True), \
                self.assertRaises(MattermostError):
            await self.client.post("c1", "", "once")
        self.assertEqual(len(self.server.posts), 1)  # a late retry would have posted it twice

    async def test_pending_create_conflict_is_retried(self):
        self.server.fail("POST", "posts", 500, body={"id": "api.post.deduplicate_create_post.pending",
                                                     "status_code": 500})
        await self.client.post("c1", "", "hello")
        self.assertEqual(len(self.server.posts), 1)

    async def test_patch_retries_5xx_and_429_with_retry_after(self):
        post = await self.client.post("c1", "", "x")
        self.server.fail("PUT", "posts", 503)
        self.server.fail("PUT", "posts", 429, body="limit exceeded\n", headers={"Retry-After": "1"})
        started = time.monotonic()
        await self.client.patch(post["id"], "y")
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        self.assertEqual(self.server.stored[post["id"]]["message"], "y")

    async def test_retry_after_alone_sets_the_wait(self):
        # One 429 each; the client's waits are recorded instead of taken, to stay fast. Without
        # a usable Retry-After, the wait before the second attempt is 0.4-0.6 s.
        post = await self.client.post("c1", "", "x")
        waits, real_sleep = [], asyncio.sleep

        async def sleep(delay, result=None):
            waits.append(delay)
            return await real_sleep(0, result)

        async def wait_after(retry_after):
            waits.clear()
            self.server.fail("PUT", "posts", 429, body="limit exceeded\n", headers={"Retry-After": retry_after})
            await self.client.patch(post["id"], retry_after)
            return sum(waits)

        with patch("aelix_mattermost.mattermost.asyncio.sleep", sleep):
            waited = {value: await wait_after(value) for value in ("2.5", "0")}
        with self.subTest(retry_after="2.5"):
            self.assertGreaterEqual(waited["2.5"], 2.4)
        with self.subTest(retry_after="0"):
            self.assertLess(waited["0"], 0.4)
        self.assertEqual(self.server.stored[post["id"]]["message"], "0")

    async def test_retries_are_bounded_and_4xx_is_not_retried(self):
        post = await self.client.post("c1", "", "x")
        self.server.fail("PUT", "posts", 503, times=3, headers={"Retry-After": "0"})
        with self.assertRaises(MattermostError) as caught:
            await self.client.patch(post["id"], "y")
        self.assertEqual(caught.exception.status, 503)
        self.server.fail("PUT", "posts", 400, times=2)
        with self.assertRaises(MattermostError):
            await self.client.patch(post["id"], "y")
        self.assertEqual(self.server.failures[0]["times"], 1)
        self.server.failures.clear()
        self.server.fail("POST", "posts", 503, times=2)  # no pending_post_id: never repeated
        with self.assertRaises(MattermostError):
            await self.client.api("POST", "posts", {"channel_id": "c1", "message": "x"})
        self.assertEqual(self.server.failures[0]["times"], 1)

    async def test_401_is_authentication_and_403_is_forbidden(self):
        post = await self.client.post("c1", "", "x")
        await self.client.delete_post(post["id"])
        with self.assertRaises(ForbiddenError) as caught:
            await self.client.patch(post["id"], "edit of a deleted post")
        self.assertNotIsInstance(caught.exception, AuthenticationError)
        self.assertIsInstance(caught.exception, MattermostError)
        self.server.auth_error = True
        with self.assertRaises(AuthenticationError) as denied:
            await self.client.me()
        self.assertEqual(denied.exception.status, 401)

    async def test_delete_post_is_idempotent(self):
        post = await self.client.post("c1", "", "x")
        await self.client.delete_post(post["id"])
        await self.client.delete_post(post["id"])
        self.assertEqual(self.server.deletes, [{"id": post["id"]}])
        self.server.fail("DELETE", "posts", 403)
        with self.assertRaises(ForbiddenError):
            await self.client.delete_post("other")

    # -- WebSocket ---------------------------------------------------------------

    async def test_header_auth_hello_and_connection_flags(self):
        self.assertFalse(self.client.connected)
        queue, _ = self.consume()
        await self.until(lambda: self.client.connected)
        self.assertAlmostEqual(self.client.connected_since, time.time(), delta=5)
        await self.server.push_event(event())
        self.assertEqual((await asyncio.wait_for(queue.get(), 2))["seq"], 1)
        self.assertAlmostEqual(self.client.last_event_at, time.time(), delta=5)
        self.assertEqual(self.server.actions, [])  # no authentication_challenge, no ping
        await self.server.drop_connections()
        await self.until(lambda: self.server.live == 1)
        self.assertEqual(self.server.challenges, 0)

    async def test_websocket_auth_failure_is_terminal(self):
        self.server.auth_error = True  # the upgrade is anonymous and closed without a frame
        started = time.monotonic()
        _, task = self.consume()
        with self.assertRaises(AuthenticationError):
            await asyncio.wait_for(task, 5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(self.client.connected)

    async def test_handshake_rejection_is_terminal(self):
        for status in (401, 403):
            self.server.upgrade_status = status
            with self.subTest(status=status), self.assertRaises(AuthenticationError):
                await asyncio.wait_for(anext(self.client.events()), 3)

    async def test_close_before_hello_reconnects_while_rest_accepts_the_token(self):
        self.server.close_before_hello = 1
        self.server.websocket_events = [event()]
        queue, _ = self.consume()
        with self.assertLogs("aelix_mattermost.mattermost", "WARNING") as logs:
            first = await asyncio.wait_for(queue.get(), 3)
        self.assertEqual(json.loads(first["data"]["post"])["id"], "p1")
        self.assertEqual(self.server.connections, 2)
        self.assertIn("before hello", "\n".join(logs.output))

    async def test_missing_hello_times_out_and_reconnects(self):
        self.server.hello_delay = None
        self.client.hello_timeout = 0.3
        self.server.websocket_events = [event()]
        queue, _ = self.consume()
        await self.until(lambda: self.server.connections >= 1)
        self.server.hello_delay = 0
        await asyncio.wait_for(queue.get(), 3)
        self.assertGreaterEqual(self.server.connections, 2)

    async def test_resume_replays_lost_and_queued_events_once(self):
        queue, _ = self.consume()
        await self.until(lambda: self.client.connected)
        await self.server.push_event(event("p1"))
        await asyncio.wait_for(queue.get(), 2)
        self.server.lose_events = 1
        await self.server.push_event(event("p2"))  # written to nobody: lost in transit
        await self.server.drop_connections()
        await self.server.push_event(event("p3"))  # queued while disconnected
        with self.assertLogs("aelix_mattermost.mattermost", "INFO") as logs:
            replayed = [await asyncio.wait_for(queue.get(), 3) for _ in range(2)]
        self.assertIn("resumed", "\n".join(logs.output))
        self.assertNotIn("missed", "\n".join(logs.output))
        self.assertEqual([json.loads(x["data"]["post"])["id"] for x in replayed], ["p2", "p3"])
        self.assertEqual([x["seq"] for x in replayed], [2, 3])
        first, second = self.server.upgrades
        self.assertEqual(first, {})
        self.assertEqual(second["sequence_number"], "2")
        self.assertEqual(len(second["connection_id"]), 26)
        await self.server.push_event(event("p4"))
        self.assertEqual((await asyncio.wait_for(queue.get(), 2))["seq"], 4)
        self.assertTrue(queue.empty())

    async def test_lossless_resume_is_confirmed_by_ping_without_hello(self):
        self.client.hello_timeout = 5
        self.consume()
        await self.until(lambda: self.client.connected)
        before = self.client.connected_since
        started = time.monotonic()
        await self.server.drop_connections()
        await self.until(lambda: self.client.connected and self.client.connected_since != before, timeout=2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual([x["action"] for x in self.server.actions], ["ping"])
        self.assertEqual(self.server.challenges, 0)

    async def test_websocket_authentication_and_reconnect_duplicate(self):
        # A server restart loses the session: the new hello carries a new connection_id,
        # events may be redelivered, and the gateway's post-id claim drops the duplicate.
        self.server.websocket_events = [event()]
        queue, _ = self.consume()
        first = await asyncio.wait_for(queue.get(), 2)
        with self.assertLogs("aelix_mattermost.mattermost", "WARNING") as logs:
            await self.server.restart()
            second = await asyncio.wait_for(queue.get(), 3)
        self.assertIn("events may have been missed", "\n".join(logs.output))
        self.assertEqual(first["data"], second["data"])
        self.assertIn("connection_id", self.server.upgrades[-1])
        self.assertEqual(self.server.challenges, 0)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            post_id = json.loads(second["data"]["post"])["id"]
            self.assertTrue(store.claim(post_id))
            self.assertFalse(store.claim(post_id))
            store.close()

    async def test_failed_resume_starts_a_new_session(self):
        queue, _ = self.consume()
        await self.until(lambda: self.client.connected)
        self.server.close_before_hello = 1
        self.server.websocket_events = [event()]
        await self.server.drop_connections()
        await asyncio.wait_for(queue.get(), 3)
        self.assertIn("connection_id", self.server.upgrades[1])
        self.assertNotIn("connection_id", self.server.upgrades[2])

    async def test_oversized_event_starts_a_new_session_instead_of_a_replay_loop(self):
        with patch("aelix_mattermost.mattermost.MAX_WS_MESSAGE", 1024):
            queue, _ = self.consume()
            await self.until(lambda: self.client.connected)
            await self.server.push_event(event(text="x" * 4096))
            await self.until(lambda: len(self.server.upgrades) == 2 and self.client.connected)
        self.assertNotIn("connection_id", self.server.upgrades[1])
        self.assertTrue(queue.empty())

    async def test_probe_websocket(self):
        hello = await self.client.probe_websocket(2)
        self.assertEqual(len(hello["connection_id"]), 26)
        self.assertEqual(hello["server_version"], "11.11.1")
        self.assertFalse(self.client.connected)
        self.server.hello_delay = None
        with self.assertRaises(MattermostError):
            await self.client.probe_websocket(0.3)
        self.server.upgrade_status = 502
        with self.assertRaises(MattermostError) as caught:
            await self.client.probe_websocket(1)
        self.assertEqual(caught.exception.status, 502)
        self.server.upgrade_status, self.server.auth_error = None, True
        with self.assertRaises(AuthenticationError):
            await self.client.probe_websocket(2)


class FixtureRulesTests(unittest.IsolatedAsyncioTestCase):
    """The double must keep following websocket_router.go, or the tests above prove nothing."""

    async def asyncSetUp(self):
        self.server = MattermostFixture()
        await self.server.start()
        self.url = self.server.url.replace("http", "ws", 1) + "/api/v4/websocket"

    async def asyncTearDown(self):
        await self.server.close()

    async def receive(self, websocket, timeout=1.0):
        message = await asyncio.wait_for(websocket.receive(), timeout)
        return json.loads(message.data) if message.type == aiohttp.WSMsgType.TEXT else message.type

    async def test_challenge_authenticates_an_anonymous_upgrade(self):
        async with aiohttp.ClientSession() as session, session.ws_connect(self.url) as websocket:
            with self.assertRaises(TimeoutError):  # nothing is sent before authentication
                await self.receive(websocket, 0.1)
            await websocket.send_json({"seq": 1, "action": "authentication_challenge",
                                       "data": {"token": "test-secret"}})
            self.assertEqual((await self.receive(websocket))["event"], "hello")
            self.assertEqual(await self.receive(websocket), {"status": "OK", "seq_reply": 1})

    async def test_challenge_is_ignored_on_a_header_authenticated_upgrade(self):
        headers = {"Authorization": "Bearer test-secret"}
        async with aiohttp.ClientSession(headers=headers) as session, session.ws_connect(self.url) as websocket:
            self.assertEqual((await self.receive(websocket))["seq"], 0)
            await websocket.send_json({"seq": 1, "action": "authentication_challenge",
                                       "data": {"token": "test-secret"}})
            await websocket.send_json({"seq": 2, "action": "ping"})
            reply = await self.receive(websocket)
            self.assertEqual((reply["seq_reply"], reply["data"]["text"]), (2, "pong"))

    async def test_invalid_or_missing_authentication_closes_without_a_frame(self):
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.url) as websocket:
                await websocket.send_json({"seq": 1, "action": "authentication_challenge", "data": {"token": "x"}})
                self.assertIn(await self.receive(websocket), {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR})
            async with session.ws_connect(self.url) as websocket:
                await websocket.send_json({"seq": 1, "action": "ping"})  # unregistered: no reply
                self.assertIn(await self.receive(websocket), {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR})


class StandaloneFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_serves_events_and_records_posts_until_killed(self):
        with tempfile.TemporaryDirectory() as directory:
            events, record = Path(directory) / "events.json", Path(directory) / "record.jsonl"
            events.write_text(json.dumps([event(text="from file")]))
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(FIXTURE), "--port", "0", "--token", "smoke", "--events", str(events),
                "--record", str(record), stdout=asyncio.subprocess.PIPE,
            )
            try:
                line = (await asyncio.wait_for(process.stdout.readline(), 10)).decode()
                url = line.rsplit(" ", 1)[-1].strip()
                config = Config(url=url, token="smoke", allowed_users=("u1",), allow_insecure_http=True).validate()
                async with MattermostClient(config) as client:
                    iterator = client.events()
                    received = await asyncio.wait_for(anext(iterator), 5)
                    await iterator.aclose()
                    self.assertEqual(json.loads(received["data"]["post"])["message"], "from file")
                    post = await client.post("c1", "p1", "answer @all")
                    await client.patch(post["id"], "edited")
                    await client.delete_post(post["id"])
            finally:
                process.terminate()
                await asyncio.wait_for(process.wait(), 10)
            lines = [json.loads(x) for x in record.read_text().splitlines()]
            self.assertEqual([x["action"] for x in lines], ["create", "patch", "delete"])
            self.assertEqual(lines[0]["message"], f"answer @{W}all")
            self.assertEqual(lines[0]["props"], PROPS)
            self.assertEqual(process.returncode, 0)


if __name__ == "__main__":
    unittest.main()
