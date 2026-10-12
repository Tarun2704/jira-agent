import hashlib
import hmac
import time

from app import slack_client as sc

SECRET = "shh"


def _sig(body: bytes, ts: str, secret: str = SECRET) -> str:
    return "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()


def test_signature_valid_and_invalid():
    ts = str(int(time.time()))
    body = b'{"a":1}'
    assert sc.verify_signature(SECRET, ts, body, _sig(body, ts))
    assert not sc.verify_signature(SECRET, ts, body, _sig(body, ts, "other"))
    assert not sc.verify_signature(SECRET, ts, b"tampered", _sig(body, ts))
    assert not sc.verify_signature("", ts, body, _sig(body, ts, ""))
    assert not sc.verify_signature(SECRET, "abc", body, "v0=x")


def test_signature_rejects_replays():
    old = str(int(time.time()) - 3600)
    body = b"{}"
    assert not sc.verify_signature(SECRET, old, body, _sig(body, old))


def test_to_mrkdwn():
    out = sc.to_mrkdwn("## Plan\n1. **Fix** `a.py` see [docs](https://x.io/a)")
    assert out == "*Plan*\n1. *Fix* `a.py` see <https://x.io/a|docs>"


def test_plan_blocks_have_proceed_button_and_respect_limits():
    body = "## Plan\n" + "\n".join(f"{i}. step " + "x" * 200 for i in range(60))
    blocks = sc.plan_blocks("CA-1", "Fix login", body, 2, questions_open=True)
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section")
    assert len(blocks) <= 50
    button = blocks[-1]["elements"][0]
    assert button["action_id"] == sc.ACTION_PROCEED and button["value"] == "CA-1"
    assert "v2" in blocks[0]["text"]["text"]
    assert "questions" in blocks[-2]["elements"][0]["text"]


def test_single_enormous_line_is_split_in_order():
    blocks = sc.plan_blocks("CA-1", "t", "short\n" + "y" * 7000, 1, False)
    texts = [b["text"]["text"] for b in blocks if b["type"] == "section"][1:]
    assert texts[0] == "short" and "".join(texts[1:]) == "y" * 7000


def test_confirm_blocks_warn_about_open_questions():
    b = sc.confirm_blocks("CA-1", "t", questions_open=True)
    assert "can't be stopped" in b[0]["text"]["text"] and "unanswered" in b[0]["text"]["text"]
    ids = [e["action_id"] for e in b[1]["elements"]]
    assert ids == [sc.ACTION_CONFIRM, sc.ACTION_BACK]
