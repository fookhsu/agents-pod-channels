"""Offline tests for the deliverer's message rules (#82, ADR 0025).

Nothing here touches the network or a store: `feishu_client.api`,
`upload_image` and `tenant_token` are stubbed at the module seam, so the tests
drive `feishu_deliver.main()` with hand-written entries and assert on the calls
a real run would make — plus `upload_image`'s own contract against a stubbed
`_request` (multipart shape, the 10 MB / non-empty cap).

Run from this directory: python3 -m unittest test_feishu_deliver -v
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import feishu_client as fc
import feishu_deliver

ADDRESS = "chat=oc_chat;affinity=om_root;thread=om_thread"
UUID = "a" * 50


class Capture:
    """Records every api/upload call a run makes; answers success."""

    def __init__(self):
        self.calls = []
        self.uploads = []

    def api(self, method, path, token=None, body=None, params=None, timeout=10):
        self.calls.append({"method": method, "path": path, "body": body, "params": params})
        return {}

    def upload_image(self, token, path, timeout=30):
        self.uploads.append(path)
        return "img_v3_test"

    def tenant_token(self, force=False):
        return "t-token"


def run_main(entry, capture, uuid=UUID):
    """Feeds one entry to stdin; returns (exit code, captured stderr)."""
    stderr = io.StringIO()
    with mock.patch.object(fc, "api", capture.api), \
         mock.patch.object(fc, "upload_image", capture.upload_image), \
         mock.patch.object(fc, "tenant_token", capture.tenant_token), \
         mock.patch.dict(os.environ, {"AGENTS_POD_OUTBOX_UUID": uuid}), \
         mock.patch("sys.stdin", io.StringIO(json.dumps(entry))), \
         redirect_stdout(io.StringIO()), \
         redirect_stderr(stderr):
        code = feishu_deliver.main()
    return code, stderr.getvalue()


class DeliverImageTests(unittest.TestCase):
    def test_text_only_stays_one_text_message(self):
        capture = Capture()
        code, _ = run_main({"to": ADDRESS, "text": "words"}, capture)
        self.assertEqual(code, 0)
        self.assertEqual(len(capture.calls), 1)
        call = capture.calls[0]
        self.assertIn("/reply", call["path"])
        self.assertEqual(call["body"]["msg_type"], "text")
        self.assertEqual(call["body"]["uuid"], UUID, "the uuid rides in the body, where dedup lives")
        self.assertEqual(capture.uploads, [])

    def test_image_uploads_then_sends_as_a_second_message(self):
        capture = Capture()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shot.png")
            with open(path, "wb") as fh:
                fh.write(b"png")
            code, _ = run_main({"to": ADDRESS, "text": "see attached", "image": path}, capture)
        self.assertEqual(code, 0)
        self.assertEqual(capture.uploads, [path])
        self.assertEqual(len(capture.calls), 2)
        text, image = capture.calls
        self.assertEqual(text["body"]["msg_type"], "text")
        self.assertEqual(image["body"]["msg_type"], "image")
        self.assertEqual(json.loads(image["body"]["content"]), {"image_key": "img_v3_test"})
        self.assertEqual(image["body"]["uuid"], UUID[:46] + "-img", "a derived second idempotency key")
        self.assertEqual(image["path"], text["path"], "same reply-first addressing")

    def test_relative_image_resolves_against_cwd(self):
        capture = Capture()
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                with open("shot.png", "wb") as fh:
                    fh.write(b"png")
                expected = os.path.join(os.getcwd(), "shot.png")  # getcwd() resolves symlinks; abspath does not
                code, _ = run_main({"to": ADDRESS, "text": "x", "image": "shot.png"}, capture)
            finally:
                os.chdir(old)
        self.assertEqual(code, 0)
        self.assertEqual(capture.uploads, [expected])

    def test_missing_image_fails_after_the_text_with_one_text_send(self):
        capture = Capture()
        missing = "/nonexistent/agents-pod-probe.png"
        stderr = io.StringIO()
        # The real upload_image — the missing path must fail at stat, at the
        # domain seam, before any request is built.
        with mock.patch.object(fc, "api", capture.api), \
             mock.patch.object(fc, "tenant_token", capture.tenant_token), \
             mock.patch.dict(os.environ, {"AGENTS_POD_OUTBOX_UUID": UUID}), \
             mock.patch("sys.stdin", io.StringIO(json.dumps(
                 {"to": ADDRESS, "text": "x", "image": missing}))), \
             redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = feishu_deliver.main()
        self.assertEqual(code, 1)
        self.assertEqual(len(capture.calls), 1, "the text went out once; the entry retries it deduped")
        self.assertIn(missing, stderr.getvalue())

    def test_non_string_image_is_refused_before_anything_sends(self):
        capture = Capture()
        code, err = run_main({"to": ADDRESS, "text": "x", "image": 42}, capture)
        self.assertEqual(code, 1)
        self.assertEqual(capture.calls, [])
        self.assertIn("non-empty path", err)

    def test_affinity_failure_falls_back_to_the_chat_for_both_messages(self):
        capture = Capture()
        real_api = capture.api
        state = {"n": 0}

        def failing_reply(method, path, token=None, body=None, params=None, timeout=10):
            state["n"] += 1
            if "/reply" in path:
                raise fc.FeishuError(path, 230011, "gone")
            return real_api(method, path, token=token, body=body, params=params, timeout=timeout)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shot.png")
            with open(path, "wb") as fh:
                fh.write(b"png")
            with mock.patch.object(fc, "api", failing_reply), \
                 mock.patch.object(fc, "upload_image", capture.upload_image), \
                 mock.patch.object(fc, "tenant_token", capture.tenant_token), \
                 mock.patch.dict(os.environ, {"AGENTS_POD_OUTBOX_UUID": UUID}), \
                 mock.patch("sys.stdin", io.StringIO(json.dumps(
                     {"to": ADDRESS, "text": "x", "image": path}))), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = feishu_deliver.main()
        self.assertEqual(code, 0)
        sends = [call for call in capture.calls if "/reply" not in call["path"]]
        self.assertEqual(len(sends), 2, "text and image both land in the chat")
        for send in sends:
            self.assertEqual(send["path"], "/im/v1/messages")
            self.assertEqual(send["body"]["receive_id"], "oc_chat")


class UploadImageTests(unittest.TestCase):
    """`upload_image`'s own contract, against a stubbed transport."""

    def setUp(self):
        self.answer = {"code": 0, "data": {"image_key": "img_v3_test"}}
        self.captured = {}

        def fake_request(method, path, token=None, body=None, params=None,
                         timeout=10, data=None, content_type=None):
            self.captured = {"data": data, "content_type": content_type, "token": token}
            return self.answer

        self.request_patcher = mock.patch.object(fc, "_request", fake_request)
        self.request_patcher.start()
        self.addCleanup(self.request_patcher.stop)

    def test_multipart_shape_and_returned_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shot.png")
            with open(path, "wb") as fh:
                fh.write(b"PNGDATA")
            key = fc.upload_image("t-token", path)
        self.assertEqual(key, "img_v3_test")
        self.assertIn("multipart/form-data; boundary=", self.captured["content_type"])
        self.assertIn("image_type", self.captured["data"].decode())
        self.assertIn(b'filename="shot.png"', self.captured["data"])
        self.assertIn(b"PNGDATA", self.captured["data"])

    def test_missing_file_is_a_domain_error(self):
        with self.assertRaises(fc.FeishuError) as ctx:
            fc.upload_image("t-token", "/nonexistent/agents-pod.png")
        self.assertEqual(ctx.exception.code, "image-unreadable")

    def test_empty_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "empty.png")
            open(path, "wb").close()
            with self.assertRaises(fc.FeishuError) as ctx:
                fc.upload_image("t-token", path)
        self.assertEqual(ctx.exception.code, "image-size")

    def test_platform_error_carries_the_code(self):
        self.answer = {"code": 99991672, "msg": "no permission"}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shot.png")
            with open(path, "wb") as fh:
                fh.write(b"png")
            with self.assertRaises(fc.FeishuError) as ctx:
                fc.upload_image("t-token", path)
        self.assertEqual(ctx.exception.code, 99991672)


if __name__ == "__main__":
    unittest.main()
