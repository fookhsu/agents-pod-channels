"""Shared Feishu client for the agents-pod operator adapters (ADR 0024 / 0025).

No third-party dependencies: urllib only, so a cron tick can run an adapter
without a virtualenv. Credentials come from this directory's `.env` (0600) and
never appear on a command line or in stdout.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://open.feishu.cn/open-apis"
HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, ".env")
TOKEN_FILE = os.path.join(HERE, ".token.json")
STATE_FILE = os.path.join(HERE, "state.json")
BOT_FILE = os.path.join(HERE, ".bot.json")


class FeishuError(RuntimeError):
    def __init__(self, path, code, msg, ext=None):
        super().__init__("%s: code=%s msg=%s" % (path, code, msg))
        self.path = path
        self.code = code
        self.msg = msg
        # Feishu puts the actionable part - the scope name, often a console
        # link - in `ext` rather than `msg`, so keep it (ADR 0027's --check).
        self.ext = ext or ""


def load_env():
    """Reads `.env` without clobbering the environment operators set explicitly."""
    values = {}
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    for key, val in values.items():
        os.environ.setdefault(key, val)
    return values


def _request(method, path, token=None, body=None, params=None, timeout=10,
             data=None, content_type=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    headers = {"Content-Type": content_type or "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    raw = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", "replace")
            if err.code == 429 and attempt < 2:
                time.sleep(1 + attempt)
                continue
            break
        except urllib.error.URLError as err:
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise FeishuError(path, "network", str(err))
    try:
        return json.loads(raw or "{}")
    except ValueError:
        raise FeishuError(path, "not-json", (raw or "")[:200])


def api(method, path, token=None, body=None, params=None, timeout=10):
    answer = _request(method, path, token, body, params, timeout)
    code = answer.get("code")
    if code not in (0, None):
        raise FeishuError(path, code, answer.get("msg"), answer.get("ext"))
    return answer.get("data", answer)


# The platform's own cap on one uploaded image (open.feishu.cn im-v1/image):
# at most 10 MB and never 0 bytes; a bigger file must travel as a `file`
# message instead.
IMAGE_MAX_BYTES = 10 * 1024 * 1024


def upload_image(token, path, timeout=30):
    """POST /im/v1/images — one image in, its `image_key` out (#82).

    urllib multipart, no third-party dependency (the client's one rule).
    `image_type` is fixed to `message`: the deliverer only ever sends into a
    chat. Returns the key the send/reply APIs embed in `content`.
    """
    try:
        size = os.path.getsize(path)
    except OSError as err:
        raise FeishuError("/im/v1/images", "image-unreadable", str(err))
    if size == 0 or size > IMAGE_MAX_BYTES:
        raise FeishuError("/im/v1/images", "image-size",
                          "%d bytes; the platform allows 1 B to 10 MB" % size)
    boundary = "----agents-pod-%d" % time.time_ns()
    name = os.path.basename(path).encode("utf-8")
    with open(path, "rb") as fh:
        payload = fh.read()
    parts = [
        ("--%s\r\n" % boundary).encode(),
        b'Content-Disposition: form-data; name="image_type"\r\n\r\n',
        b"message\r\n",
        ("--%s\r\n" % boundary).encode(),
        b'Content-Disposition: form-data; name="image"; filename="',
        name,
        b"\"\r\n",
        b"Content-Type: application/octet-stream\r\n\r\n",
        payload,
        ("\r\n--%s--\r\n" % boundary).encode(),
    ]
    answer = _request("POST", "/im/v1/images", token=token, data=b"".join(parts),
                      content_type="multipart/form-data; boundary=%s" % boundary,
                      timeout=timeout)
    code = answer.get("code")
    if code not in (0, None):
        raise FeishuError("/im/v1/images", code, answer.get("msg"), answer.get("ext"))
    image_key = (answer.get("data") or {}).get("image_key")
    if not image_key:
        raise FeishuError("/im/v1/images", "no-image-key", json.dumps(answer)[:200])
    return image_key


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def _write_json(path, value, mode=0o600):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _app_id():
    return os.environ.get("FEISHU_APP_ID", "").strip()


def tenant_token(force=False):
    """`tenant_access_token` lives 2 h; caching keeps a 1-minute tick cheap.

    The cache records the app it belongs to: swapping `FEISHU_APP_*` in `.env`
    must not keep using the previous app's token until it expires, or the new
    app's first failures read as missing scopes on the wrong identity.
    """
    app_id = _app_id()
    cached = _read_json(TOKEN_FILE)
    if (
        not force
        and cached.get("token")
        and cached.get("app_id") == app_id
        and cached.get("expires_at", 0) > time.time() + 60
    ):
        return cached["token"]
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise FeishuError("/auth/v3/tenant_access_token/internal", "no-credentials",
                          "FEISHU_APP_ID / FEISHU_APP_SECRET missing (see %s)" % ENV_FILE)
    answer = _request("POST", "/auth/v3/tenant_access_token/internal",
                      body={"app_id": app_id, "app_secret": app_secret})
    if answer.get("code") != 0 or not answer.get("tenant_access_token"):
        raise FeishuError("/auth/v3/tenant_access_token/internal",
                          answer.get("code"), answer.get("msg"))
    token = answer["tenant_access_token"]
    expires_at = time.time() + int(answer.get("expire", 7200))
    _write_json(TOKEN_FILE, {"token": token, "expires_at": expires_at, "app_id": app_id})
    return token


def bot_open_id(token):
    """The bot's own `open_id`, used to spot an @-mention of us.

    Cached per app: a swapped app is a different bot, and a stale id would make
    an @-mention check answer for an identity that is no longer in the chat.
    """
    app_id = _app_id()
    cached = _read_json(BOT_FILE)
    if cached.get("open_id") and cached.get("app_id") == app_id:
        return cached["open_id"]
    data = api("GET", "/bot/v3/info", token=token)
    bot = data.get("bot") or {}
    open_id = bot.get("open_id")
    if open_id:
        _write_json(BOT_FILE, {"open_id": open_id, "app_name": bot.get("app_name"),
                               "app_id": app_id})
    return open_id


def load_state():
    state = _read_json(STATE_FILE)
    state.setdefault("known_chats", [])
    state.setdefault("threads", {})
    return state


def save_state(state):
    _write_json(STATE_FILE, state)


def flatten_message(msg):
    """Plain text for a Feishu message, whatever its `msg_type`."""
    msg_type = msg.get("msg_type") or ""
    try:
        content = json.loads((msg.get("body") or {}).get("content") or "{}")
    except ValueError:
        content = {}
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return content

    if msg_type == "text":
        return str(content.get("text", "")).strip()
    if msg_type == "post":
        return _flatten_post(content).strip()
    if msg_type in ("image",):
        return "[图片]"
    if msg_type in ("file", "folder"):
        return "[文件] %s" % (content.get("file_name") or "")
    if msg_type in ("audio",):
        return "[语音]"
    if msg_type in ("media",):
        return "[视频]"
    if msg_type == "sticker":
        return "[表情]"
    if msg_type == "merge_forward":
        return "[合并转发消息]"
    if msg_type == "interactive":
        return "[卡片]"
    if msg_type == "share_chat":
        return "[群名片]"
    return "[%s]" % (msg_type or "unknown")


def _flatten_post(node):
    """A post may nest a locale layer: {zh_cn: {title, content}}."""
    if isinstance(node, dict):
        if "content" in node or "title" in node:
            lines = []
            title = (node.get("title") or "").strip()
            if title:
                lines.append(title)
            for paragraph in node.get("content") or []:
                parts = []
                for element in paragraph if isinstance(paragraph, list) else []:
                    if not isinstance(element, dict):
                        continue
                    if element.get("tag") in ("text", "a", "md"):
                        parts.append(str(element.get("text") or ""))
                    elif element.get("tag") == "at":
                        parts.append("@" + str(element.get("user_name") or "某人"))
                    elif element.get("tag") == "img":
                        parts.append("[图片]")
                if parts:
                    lines.append("".join(parts))
            return "\n".join(lines)
        for value in node.values():
            text = _flatten_post(value)
            if text:
                return text
        return ""
    if isinstance(node, list):
        return "\n".join(t for t in (_flatten_post(v) for v in node) if t)
    return ""
