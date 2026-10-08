import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from aelix_mattermost.config import Config
from aelix_mattermost.routing import mention_spans, route_event, strip_mentions


def event(post_id="p1", user="u1", channel="c1", kind="O", text="@aelix hello", root="", props=None):
    post = {"id": post_id, "user_id": user, "channel_id": channel, "message": text,
            "root_id": root, "type": "", "delete_at": 0}
    if props is not None:
        post["props"] = props
    return {"event": "posted", "data": {"channel_type": kind, "post": json.dumps(post)}}


def mentioned(text, username="aelix"):
    return bool(mention_spans(text, username))


class ServerMentionCases(unittest.TestCase):
    """Cases from server/channels/app/notification_test.go (TestGetExplicitMentions,
    TestGetExplicitMentionsAtHere) and mention_parser_standard_test.go (TestProcessText),
    v11.11.1, for a bot whose only mention keyword is "@" + its username."""

    def test_get_explicit_mentions(self):
        cases = [
            # (message, username, mentioned)
            ("this is a message", "user", False),                                  # Nobody
            ("this is a message for @user", "user", True),                         # OnePerson
            ("this is a message for @user.name.", "user.name.", True),             # ...PeriodAtEndOfUsername
            ("this is a message for @user.", "user", True),                        # OnePersonAtEndOfSentence
            ("this is a message for .@user", "user", True),                        # OnePersonWithPeriodBefore
            ("this is a message for @user:", "user", True),                        # OnePersonWithColonAfter
            ("this is a message for :@user", "user", True),                        # OnePersonWithColonBefore
            ("this is a message for -@user", "user", True),                        # OnePersonWithHyphenBefore
            ("this is an @mention for @user", "mention", True),                    # MultiplePeopleWithMultipleWords
            ("this is a message for @user.period.", "user.period", True),          # AtUserWithPeriodAtEndOfSentence
            ("this is an message for @potential.user", "potential", False),        # PotentialOutOfChannelUserWithPeriod
            ("`this shouldn't mention @user at all`", "user", False),              # InlineCode
            ("```\nthis shouldn't mention @user at all\n```", "user", False),      # FencedCodeBlock
            ("*@aaa @bbb @ccc*", "bbb", True),                                     # Emphasis
            ("**@aaa @bbb @ccc**", "ccc", True),                                   # StrongEmphasis
            ("~~@aaa @bbb @ccc~~", "aaa", True),                                   # Strikethrough
            ("### @aaa", "aaa", True),                                             # Heading
            ("> @aaa", "aaa", True),                                               # BlockQuote
            ("    this shouldn't mention @user at all", "user", False),            # IndentedCodeBlock
            ('[foo](this "shouldn\'t mention @user at all")', "user", False),     # LinkTitle
            ("`this should mention @user``", "user", True),                        # MalformedInlineCode
            ("this is an message for @user.name", "user.name", True),              # part of an actual mention
            ("this is an message for @user.name...", "user.name", True),           # multiple trailing periods
            ("this is an message for @user...name...", "user...name", True),       # containing and followed by
            ("@other @test-two", "test", False),                                   # keyword prefix of a mention
            ("@other-one @other @other-two", "other", True),
            ("@other-one @other-two", "other", False),
            ("@here @user @potential", "user", True),                              # Mention @here and someone
            ("@potential. test", "potential", True),                               # Username ending with period
        ]
        for text, username, expected in cases:
            with self.subTest(text=text, username=username):
                self.assertEqual(mentioned(text, username), expected)

    def test_at_here_boundary_cases(self):
        cases = {
            "": False, "here": False, "@here": True, " @here ": True, "\n@here\n": True,
            "!@here!": True, "#@here#": True, "$@here$": True, "%@here%": True, "^@here^": True,
            "&@here&": True, "*@here*": True, "(@here(": True, ")@here)": True, "-@here-": True,
            "_@here_": True, "=@here=": True, "+@here+": True, "[@here[": True, "{@here{": True,
            "]@here]": True, "}@here}": True, "\\@here\\": True, "|@here|": True, ";@here;": True,
            "@here:": True, ":@here:": False, "'@here'": True, '"@here"': True, ",@here,": True,
            "<@here<": True, ".@here.": True, ">@here>": True, "/@here/": True, "?@here?": True,
            "`@here`": False, "~@here~": True, "@HERE": True, "@hERe": True,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                # The same rules decide whether the bot itself is mentioned.
                self.assertEqual(mentioned(text.replace("here", "aelix").replace("HERE", "AELIX")
                                           .replace("hERe", "aELix")), expected)

    def test_process_text(self):
        for text in ("hello user @user1", "hello user.@user1", "hello user-@user1", "hello user:@user1",
                     "@user1, you can use @systembot to get help"):
            with self.subTest(text=text):
                self.assertTrue(mentioned(text, "user1"))
        self.assertFalse(mentioned("@user1, you can use @systembot to get help", "systembot2"))
        self.assertTrue(mentioned("@user1, you can use @systembot to get help", "systembot"))


class BotMentionTests(unittest.TestCase):
    def test_trailing_punctuation_the_server_strips_is_a_mention(self):
        for text in ("@aelix. hi", "@aelix... hi", "@aelix- hi", "@aelix_ hi", "@aelix: hi", ".@aelix hi",
                     "(@aelix) hi", "@aelix, hi", "hi @AELIX", "x.@aelix hi", "a\\_@aelix", "_@aelix_",
                     "> quote @aelix", "- item @aelix", "[@aelix](https://example.com) hi"):
            with self.subTest(text=text):
                self.assertTrue(mentioned(text))

    def test_words_that_only_contain_the_name_are_not_mentions(self):
        for text in ("@aelix-bot hi", "@aelix.bot hi", "email@aelix hi", "@@aelix hi", "@aelix님 안녕",
                     "@aelix에게 질문", "@aelix2 hi", ":@aelix: hi", "aelix hi", "@ aelix"):
            with self.subTest(text=text):
                self.assertFalse(mentioned(text))

    def test_code_never_mentions(self):
        for text in ("`@aelix` hi", "``x @aelix``", "```\n@aelix\n```", "~~~\n@aelix\n~~~", "    @aelix hi",
                     "para\n\n    @aelix", "- ```\n  @aelix\n  ```", "> ```\n> @aelix\n> ```",
                     "[x](https://example.com/@aelix)", '[x](u "@aelix")'):
            with self.subTest(text=text):
                self.assertFalse(mentioned(text))

    def test_bare_urls_never_mention(self):
        # markdown.InspectInline never visits an Autolink's text.
        for text in ("see https://example.com/@aelix", "www.example.com/@aelix/x", "https://x.com/`@aelix`"):
            with self.subTest(text=text):
                self.assertFalse(mentioned(text))
        self.assertTrue(mentioned("https://example.com/a. @aelix"))
        self.assertTrue(mentioned("https://example.com/<@aelix>"))

    def test_stripping_removes_mentions_outside_code_only(self):
        cases = {
            "@aelix hello": "hello",
            "@AELIX: hello": "hello",
            "Hey @aelix, how are you": "Hey , how are you",
            "Hey @aelix how are you": "Hey how are you",
            "thanks @aelix.": "thanks",
            "@aelix explain `@aelix` here": "explain `@aelix` here",
            "@aelix\n```\n@aelix code\n```": "```\n@aelix code\n```",
            "@aelix\n\n    indented @aelix code": "    indented @aelix code",
            "@aelix-bot @aelix hi": "@aelix-bot hi",
            "@aelix !help": "!help",
            "no mention here": "no mention here",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(strip_mentions(text, mention_spans(text, "aelix")), expected)

    def test_long_runs_stay_linear(self):
        # A word made of one huge suffix run must not cost quadratic time.
        self.assertTrue(mentioned("@aelix" + "." * 200000))
        self.assertFalse(mentioned("@aelixx" + "." * 200000))


class RouteEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = Config(url="http://127.0.0.1:8065", token="test-secret", allowed_users=("u1", "u2"),
                             allow_insecure_http=True, state_dir=root / "state", work_dir=root / "work",
                             command=(sys.executable,)).validate()

    def route(self, value, configuration=None):
        return route_event(value, configuration or self.config, "bot", "aelix")

    def test_integration_posts_are_ignored(self):
        # An incoming webhook posts with its owner's user_id, a slash command response with
        # its caller's; only these props tell them apart from the person's own posts.
        for name in ("from_webhook", "from_oauth_app", "from_plugin"):
            for value in ("true", True):
                with self.subTest(name=name, value=value):
                    self.assertIsNone(self.route(event(props={name: value})))
                    self.assertIsNone(self.route(event(kind="D", text="hi", props={name: value})))
        self.assertIsNotNone(self.route(event(props={"from_webhook": "false", "from_bot": "true"})))
        self.assertIsNotNone(self.route(event(props={"unrelated": "true"})))

    def test_channel_messages_need_a_server_mention(self):
        self.assertIsNone(self.route(event(text="`@aelix` hello")))
        self.assertIsNone(self.route(event(text="@aelix-bot hello")))
        self.assertIsNone(self.route(event(text="profile: https://chat.example.com/@aelix")))
        self.assertEqual(self.route(event(text="hello @aelix.")).text, "hello")
        self.assertEqual(self.route(event(text="@aelix: `@aelix` in code")).text, "`@aelix` in code")
        loose = replace(self.config, require_mention=False)
        self.assertEqual(self.route(event(text="`@aelix` hello"), loose).text, "`@aelix` hello")

    def test_dm_mentions_are_stripped_but_optional(self):
        self.assertEqual(self.route(event(kind="D", text="@aelix hi")).text, "hi")
        self.assertEqual(self.route(event(kind="D", text="email@aelix hi")).text, "email@aelix hi")

    def test_commands_follow_a_mention(self):
        for text in ("@aelix !help", "@aelix: !cancel", "!reset @aelix"):
            with self.subTest(text=text):
                self.assertIn(self.route(event(text=text)).text, {"!help", "!cancel", "!reset"})


if __name__ == "__main__":
    unittest.main()
