# -*- coding: utf-8 -*-
"""咕噜咕噜 —— TVBox T4 源。

ECC P-256 握手 + AES-GCM 会话加密 + protobuf 协议（Dart app 协议栈）。
后端 http://103.45.132.22:19987/app/bn/v2（明文 http）。

协议要点（勿删）：
  · handshake：本地生成 P-256 密钥对 → ECC ECDH 协商共享密钥 → HKDF 派生会话密钥
  · 会话后所有请求 AES-GCM 加密（nonce+ciphertext+tag），内层 zlib raw-deflate
  · 列表/详情/播放都走 POST（框架 HttpClient 只有 GET → 用 requests 自建）
  · boot() 从服务器拉 players（线路表）+ parsers（解析器表）
  · detail 有版本守卫：响应含 VERSION_GUARD_MARKERS 时拒绝（服务器要求升级）
  · play：id 格式 "play_id@parser_id@vod_name@index"，分服务端/外部解析两种

注意：容器需装 pycryptodome + requests（本机 docker exec 已装，重建容器会丢，
若重建需在 requirements.txt 加回这两项）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import zlib
from urllib.parse import quote

from Crypto.Cipher import AES
from Crypto.PublicKey import ECC
from Crypto.Util.Padding import pad, unpad

from core.base import VodDetailItem, VodHandler, VodItem, VodListResult

API_URL = "http://103.45.132.22:19987/app/bn/v2"
USER_AGENT = "Dart/3.10 (dart:io)"
APP_VERSION = "2.1.3"
BUILD_NUMBER = "20109"
APP_SIGNATURE = "32E0AB4FF93A29CE0E6F0BFB01F2F1B788E76262731F3F30F509CB822428ED58"
DEVICE_BUILD = "pangu-build-component-system-513739-s9vkd-rlxnj-p3rhm"
VERSION_GUARD_MARKERS = ("__v99_", "glgl.tv", "111.170.58.215", "shu.jpg")
FALLBACK_PARSERS = {
    28: {"name": "咕噜金牌", "url": "http://111.170.58.215:5499/api.php?id=",
         "mode": "json", "result_key": "url", "server": False},
}


def _varint(value):
    if value < 0:
        raise ValueError("negative protobuf varint")
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _field_bytes(number, value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _field_varint(number, value):
    return _varint(number << 3) + _varint(value)


def _read_varint(data, position):
    value = 0
    shift = 0
    while position < len(data) and shift < 70:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, position
        shift += 7
    raise ValueError("invalid protobuf varint")


def _parse_fields(data):
    fields = []
    position = 0
    while position < len(data):
        key, position = _read_varint(data, position)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ValueError("invalid protobuf field zero")
        if wire == 0:
            value, position = _read_varint(data, position)
        elif wire == 1:
            value = data[position:position + 8]
            position += 8
        elif wire == 2:
            size, position = _read_varint(data, position)
            value = data[position:position + size]
            position += size
        elif wire == 5:
            value = data[position:position + 4]
            position += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        if position > len(data):
            raise ValueError("truncated protobuf field")
        fields.append((number, wire, value))
    return fields


def _field_values(data, number, wire=None):
    return [v for n, w, v in _parse_fields(data)
            if n == number and (wire is None or w == wire)]


def _field_value(data, number, default=None, wire=None):
    values = _field_values(data, number, wire)
    return values[-1] if values else default


def _text(value, default=""):
    if not isinstance(value, bytes):
        return value
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return default


def _packed_varints(data):
    values = []
    position = 0
    while position < len(data):
        value, position = _read_varint(data, position)
        values.append(value)
    return values


def _raw_deflate(data):
    compressor = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-15,
                                  memLevel=8, strategy=zlib.Z_RLE)
    return compressor.compress(data) + compressor.flush()


def _derive_key(session_id, shared_x):
    prk = hmac.new(session_id.encode("ascii"), shared_x, hashlib.sha256).digest()
    return hmac.new(prk, b"v2-session\x01", hashlib.sha256).digest()


_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B


def _sec1_export_public(key):
    point = key.public_key().pointQ
    x = int(point.x).to_bytes(32, "big")
    y = int(point.y).to_bytes(32, "big")
    return b"\x04" + x + y


def _sec1_import_public(data):
    raw = bytes(data or b"")
    if len(raw) == 65 and raw[0] == 4:
        x = int.from_bytes(raw[1:33], "big")
        y = int.from_bytes(raw[33:65], "big")
    elif len(raw) == 33 and raw[0] in (2, 3):
        x = int.from_bytes(raw[1:], "big")
        y2 = (pow(x, 3, _P256_P) - 3 * x + _P256_B) % _P256_P
        y = pow(y2, (_P256_P + 1) // 4, _P256_P)
        if (y & 1) != (raw[0] & 1):
            y = _P256_P - y
    else:
        raise ValueError("invalid P-256 SEC1 public key")
    return ECC.construct(curve="P-256", point_x=x, point_y=y)


class _GuluProtocol:
    def __init__(self, timeout=15):
        self.timeout = timeout
        self.session_id = ""
        self.session_key = b""
        self.device_id = secrets.token_hex(8)
        self.request_id = 0
        self.players = {}
        self.parsers = {}
        self.last_detail_error = ""
        self.lock = threading.RLock()

    def _post(self, body, header_name, header_value, extra_headers=None):
        import requests
        headers = {
            "user-agent": USER_AGENT, header_name: header_value,
            "content-type": "application/x-protobuf", "accept-encoding": "gzip",
            "content-length": str(len(body)), "host": "103.45.132.22:19987",
        }
        if extra_headers:
            headers.update(extra_headers)
        resp = requests.post(API_URL, data=body, headers=headers,
                             timeout=self.timeout, stream=False)
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError("HTTP %s: %s" % (resp.status_code, resp.text[:200]))
        return resp.content

    def handshake(self):
        private_key = ECC.generate(curve="P-256")
        public_key = _sec1_export_public(private_key)
        capabilities = _field_varint(1, 1) + _field_varint(2, 0) + _field_varint(3, 1)
        request = (_field_bytes(1, public_key) + _field_bytes(2, b"1.0.0")
                   + _field_bytes(3, capabilities))
        handshake_key = secrets.token_hex(16)
        iv = secrets.token_bytes(16)
        cipher = AES.new(handshake_key.encode("ascii"), AES.MODE_CBC, iv)
        encrypted = base64.b64encode(
            iv + cipher.encrypt(pad(zlib.compress(request, 4), AES.block_size)))
        content = self._post(encrypted, "x-handshake-key", handshake_key)
        raw = base64.b64decode(content)
        response_cipher = AES.new(handshake_key.encode("ascii"), AES.MODE_CBC, raw[:16])
        plain = zlib.decompress(unpad(response_cipher.decrypt(raw[16:]), AES.block_size))
        session_id = _field_value(plain, 1, wire=2)
        server_public = _field_value(plain, 2, wire=2)
        if not session_id or not server_public:
            raise RuntimeError("invalid handshake response")
        self.session_id = session_id.decode("ascii")
        server_key = _sec1_import_public(server_public)
        shared_point = server_key.pointQ * int(private_key.d)
        shared_x = int(shared_point.x).to_bytes(32, "big")
        self.session_key = _derive_key(self.session_id, shared_x)
        self.request_id = 0

    def _encrypt(self, data):
        nonce = secrets.token_bytes(12)
        cipher = AES.new(self.session_key, AES.MODE_GCM, nonce=nonce, mac_len=16)
        ciphertext, tag = cipher.encrypt_and_digest(_raw_deflate(data))
        return nonce + ciphertext + tag

    def _decrypt(self, data):
        if len(data) < 28:
            raise ValueError("invalid GCM payload")
        nonce, ciphertext, tag = data[:12], data[12:-16], data[-16:]
        compressed = AES.new(self.session_key, AES.MODE_GCM, nonce=nonce,
                             mac_len=16).decrypt_and_verify(ciphertext, tag)
        return zlib.decompress(compressed, -15)

    def request(self, method, payload=b"", scope=3, extra_headers=None):
        with self.lock:
            if not self.session_id:
                self.handshake()
            self.request_id += 1
            request_id = self.request_id
            core = (_field_varint(1, request_id) + _field_varint(2, scope)
                    + _field_varint(3, method) + _field_bytes(4, self.device_id)
                    + _field_bytes(5, b"") + _field_bytes(6, payload)
                    + _field_varint(7, int(time.time() * 1000)))
            body = _field_varint(1, request_id) + _field_bytes(2, self._encrypt(core))
            content = self._post(body, "x-session-id", self.session_id,
                                 extra_headers=extra_headers)
            encrypted = _field_value(content, 2, wire=2)
            if encrypted is None:
                error = _text(_field_value(content, 3, b"", wire=2))
                raise RuntimeError(error or "server rejected request")
            plain = self._decrypt(encrypted)
            status = _text(_field_value(plain, 3, b"", wire=2))
            if status and status not in ("ok", "success"):
                raise RuntimeError(status)
            return plain

    def boot(self):
        now_us = int(time.time() * 1_000_000)
        app = (_field_bytes(1, "咕噜咕噜") + _field_bytes(2, APP_VERSION)
               + _field_bytes(3, b"com.himrsc.viz") + _field_bytes(4, APP_SIGNATURE)
               + _field_bytes(5, BUILD_NUMBER) + _field_varint(6, now_us)
               + _field_varint(7, now_us))
        device = (_field_bytes(1, self.device_id) + _field_varint(2, 1)
                  + _field_bytes(3, b"15") + _field_bytes(4, b"Redmi K50 Ultra")
                  + _field_bytes(5, b"Redmi/diting/diting:15/AQ3A.240912.001/OS2.0.215.0.VOACNXM:user/release-keys")
                  + _field_bytes(6, b"Redmi") + _field_bytes(7, b"qcom")
                  + _field_varint(8, 0) + _field_bytes(9, b"unknown")
                  + _field_bytes(10, DEVICE_BUILD) + _field_varint(11, 0)
                  + _field_varint(12, 35))
        payload = (_field_bytes(1, b"v2") + _field_bytes(2, b"android")
                   + _field_bytes(3, b"gulu") + _field_bytes(4, app)
                   + _field_bytes(5, device))
        response = self.request(0, payload, scope=1)
        config = _field_value(response, 4, b"", wire=2)
        players = {}
        for item in _field_values(config, 4, wire=2):
            code = _text(_field_value(item, 3, b"", wire=2))
            if not code:
                continue
            parser_ids = []
            for packed in _field_values(item, 8, wire=2):
                parser_ids.extend(_packed_varints(packed))
            players[code] = {"id": _field_value(item, 1, 0, wire=0),
                             "name": _text(_field_value(item, 4, b"", wire=2)),
                             "parser_ids": parser_ids}
        parsers = {}
        for item in _field_values(config, 7, wire=2):
            parser_id = _field_value(item, 1, 0, wire=0)
            if parser_id:
                parsers[parser_id] = {
                    "name": _text(_field_value(item, 2, b"", wire=2)),
                    "url": _text(_field_value(item, 3, b"", wire=2)),
                    "mode": _text(_field_value(item, 4, b"", wire=2)),
                    "result_key": _text(_field_value(item, 10, b"url", wire=2)),
                    "server": bool(_field_value(item, 20, 0, wire=0))}
        self.players = players
        self.parsers = parsers

    def search(self, keyword, page=1, limit=21, category_id=""):
        filters = _field_bytes(18, b"vod_hits_month") + _field_varint(19, 1)
        category_id = str(category_id or "").strip()
        if category_id:
            filters += _field_bytes(3, category_id) + _field_bytes(4, category_id)
        payload = (_field_bytes(1, keyword) + _field_varint(2, int(page))
                   + _field_varint(3, int(limit)) + _field_bytes(5, filters))
        response = self.request(61, payload)
        data = _field_value(response, 4, b"", wire=2)
        videos = []
        for message in _field_values(data, 1, wire=2):
            videos.append({
                "vod_id": str(_field_value(message, 1, 0, wire=0)),
                "vod_name": _text(_field_value(message, 3, b"", wire=2)),
                "vod_pic": _text(_field_value(message, 6, b"", wire=2)),
                "vod_remarks": _text(_field_value(message, 11, b"", wire=2)),
            })
        page_info = _field_value(data, 2, b"", wire=2)
        return {"videos": videos,
                "page": _field_value(page_info, 1, int(page), wire=0),
                "pagecount": _field_value(page_info, 3, 1, wire=0),
                "limit": _field_value(page_info, 2, int(limit), wire=0),
                "total": _field_value(page_info, 4, len(videos), wire=0)}

    def detail(self, vod_id):
        self.last_detail_error = ""
        payload = (_field_varint(1, int(vod_id)) + _field_bytes(3, APP_VERSION)
                   + _field_bytes(4, b"1") + _field_varint(5, 1))
        response = self.request(62, payload,
                                extra_headers={"x-player-page-protection": "1"})
        data = _field_value(response, 4, b"", wire=2)
        name = _text(_field_value(data, 5, b"", wire=2))
        guard_text = " ".join(
            [_text(_field_value(data, field, b"", wire=2)) for field in (5, 13, 21)]
            + [_text(_field_value(source, 1, b"", wire=2))
               for source in _field_values(data, 75, wire=2)]).lower()
        if any(marker in guard_text for marker in VERSION_GUARD_MARKERS):
            self.last_detail_error = "server_version_guard"
            return None
        if not name or name.startswith("最新版本下载地址") or _field_value(data, 1, 0, wire=0) == 0:
            self.last_detail_error = "invalid_detail"
            return None
        sources = []
        for source in _field_values(data, 75, wire=2):
            code = _text(_field_value(source, 1, b"", wire=2))
            if not code or code.startswith("__v99_"):
                continue
            config = self.players.get(code, {})
            episodes = []
            for episode in _field_values(source, 2, wire=2):
                episode_id = _text(_field_value(episode, 3, b"", wire=2))
                if not episode_id:
                    continue
                episodes.append({
                    "index": _field_value(episode, 1, len(episodes) + 1, wire=0),
                    "id": episode_id,
                    "name": _text(_field_value(episode, 4, b"", wire=2)) or f"第{len(episodes) + 1}集"})
            if episodes:
                sources.append({"code": code,
                                "name": config.get("name", code),
                                "parser_id": (config.get("parser_ids") or [0])[0],
                                "episodes": episodes})
        return {"id": str(_field_value(data, 1, vod_id, wire=0)),
                "name": name, "pic": _text(_field_value(data, 13, b"", wire=2)),
                "remarks": _text(_field_value(data, 22, b"", wire=2)),
                "content": _text(_field_value(data, 21, b"", wire=2)),
                "area": _text(_field_value(data, 28, b"", wire=2)),
                "year": _text(_field_value(data, 30, b"", wire=2)),
                "sources": sources}

    def play(self, parser_id, play_id):
        payload = _field_varint(1, int(parser_id)) + _field_bytes(2, play_id)
        response = self.request(69, payload)
        data = _field_value(response, 4, b"", wire=2)
        return _text(_field_value(data, 2, b"", wire=2))

    def reset_session(self):
        self.session_id = ""
        self.session_key = b""
        self.request_id = 0


class Gulu(VodHandler):
    source_name = "gulu"
    display_name = "咕噜咕噜"

    HOST = "http://103.45.132.22:19987"
    UA = USER_AGENT

    _play_order = [
        "咕噜4K", "菲乐4K", "鲸宝4K", "神话", "臻影4K", "精品2K",
        "鲸宝2K", "短剧2K", "天堂", "☆讯飞☆", "☆奇趣☆", "☆果汁☆",
        "☆酷萌☆", "☆哔哩☆", "咖啡", "量子", "非凡", "暴风", "蚂蚁",
        "小熊", "海外", "花旗",
    ]
    _categories = [("1", "电影"), ("2", "电视剧"), ("3", "综艺"),
                   ("4", "动漫"), ("5", "短剧"), ("60", "直播")]

    def __init__(self) -> None:
        self.protocol = _GuluProtocol()
        self.last_error = ""

    def _ensure_boot(self) -> None:
        try:
            if not self.protocol.session_id:
                self.protocol.handshake()
            if not self.protocol.players:
                self.protocol.boot()
        except Exception as e:
            self.last_error = "%s:%s" % (type(e).__name__, e)

    def _search(self, keyword, page=1, limit=21, category_id=""):
        self._ensure_boot()
        try:
            return self.protocol.search(keyword, page, limit, category_id)
        except Exception:
            try:
                self.protocol.reset_session()
                self._ensure_boot()
                return self.protocol.search(keyword, page, limit, category_id)
            except Exception as e:
                self.last_error = "%s" % e
                return {"videos": [], "page": page, "pagecount": 1,
                        "limit": limit, "total": 0}

    # ── T4 契约 ─────────────────────────────────────────────────────

    def home(self) -> dict:
        classes = [{"type_id": tid, "type_name": nm} for tid, nm in self._categories]
        cards = []
        try:
            r = self._search("", 1, 21)
            for v in r.get("videos", [])[:20]:
                if v.get("vod_id"):
                    cards.append(VodItem(vod_id=str(v["vod_id"]),
                                         vod_name=v.get("vod_name") or "",
                                         vod_pic=v.get("vod_pic") or "",
                                         vod_remarks=v.get("vod_remarks") or "").to_dict())
        except Exception:
            pass
        return {"class": classes, "filters": {}, "list": cards}

    def category(self, type_id: str, page: str, ext: str = "") -> dict:
        pg = max(1, int(page or 1))
        result = self._search("", pg, 21, str(type_id))
        out = []
        for v in result.get("videos", []):
            if v.get("vod_id"):
                out.append(VodItem(vod_id=str(v["vod_id"]),
                                   vod_name=v.get("vod_name") or "",
                                   vod_pic=v.get("vod_pic") or "",
                                   vod_remarks=v.get("vod_remarks") or "").to_dict())
        return VodListResult(list=out, page=pg,
                             pagecount=result.get("pagecount", 1),
                             limit=result.get("limit", 21),
                             total=result.get("total", len(out))).to_dict()

    @staticmethod
    def _detail_id(ids) -> str:
        text = str(ids).strip()
        if text.isdigit():
            return text
        m = re.search(r"(?:^|[^0-9])(\d{1,12})(?:$|[^0-9])", text)
        return m.group(1) if m else ""

    @staticmethod
    def _clean_name(name: str) -> str:
        if not name:
            return ""
        c = re.sub(r'[【\[\(（].*?[】\]）)]', '', name)
        c = re.sub(r'\s*(?:HD|VIP|备用|推荐|极速|高清|标清|蓝光|超清|试看|抢先|预告|新版|旧版)\s*$',
                   '', c, flags=re.I)
        return re.sub(r'\s+', ' ', c).strip()

    @classmethod
    def _rank(cls, nm: str) -> int:
        c = cls._clean_name(nm).lower()
        for i, name in enumerate(cls._play_order):
            if c == name.lower() or c.startswith(name.lower()) or name.lower() in c:
                return i
        up = cls._clean_name(nm).upper()
        if "4K" in up:
            return 1000
        if "2K" in up:
            return 2000
        if "☆" in nm:
            return 3000
        return 4000

    def detail(self, ids: str) -> dict:
        vod_id = self._detail_id(ids)
        if not vod_id:
            return {"list": []}
        self._ensure_boot()
        try:
            detail = self.protocol.detail(vod_id)
        except Exception:
            self.protocol.reset_session()
            self._ensure_boot()
            detail = self.protocol.detail(vod_id)
        if not detail:
            return {"list": []}

        play_from, play_blocks = [], []
        for source in detail["sources"]:
            episodes = []
            for ep in source["episodes"]:
                idx = ep["index"] or len(episodes) + 1
                encoded = "{}@{}@{}@{}".format(ep["id"], source["parser_id"], detail["name"], idx)
                episodes.append(f"{ep['name']}${encoded}")
            if episodes:
                play_from.append(self._clean_name(source["name"]))
                play_blocks.append("#".join(episodes))
        paired = list(zip(play_from, play_blocks))
        paired.sort(key=lambda x: self._rank(x[0]))

        vod = VodDetailItem(
            vod_id=detail["id"],
            vod_name=detail["name"],
            vod_pic=detail["pic"],
            vod_year=detail["year"],
            vod_content=detail["content"],
            vod_remarks=detail["remarks"],
            vod_play_from="$$$".join(p[0] for p in paired),
            vod_play_url="$$$".join(p[1] for p in paired),
        )
        return {"list": [vod.to_dict()]}

    def search(self, keyword: str, page: str, ext: str = "") -> dict:
        kw = (keyword or "").strip()
        if not kw:
            return VodListResult().to_dict()
        pg = max(1, int(page or 1))
        result = self._search(kw, pg, 21)
        out = []
        for v in result.get("videos", []):
            if v.get("vod_id"):
                out.append(VodItem(vod_id=str(v["vod_id"]),
                                   vod_name=v.get("vod_name") or "",
                                   vod_pic=v.get("vod_pic") or "",
                                   vod_remarks=v.get("vod_remarks") or "").to_dict())
        return VodListResult(list=out, page=pg,
                             pagecount=result.get("pagecount", 1),
                             limit=result.get("limit", 21),
                             total=result.get("total", len(out))).to_dict()

    def player(self, flag: str, play_url: str) -> dict:
        header = {"User-Agent": USER_AGENT}
        try:
            raw_id, parser_id, vod_name, episode_index = play_url.split("@", 3)
            parser_id = int(parser_id or 0)
        except ValueError:
            raw_id, parser_id, vod_name, episode_index = play_url, 0, "", "1"
        url = raw_id if (isinstance(raw_id, str) and self._is_playable_url(raw_id)) else ""
        if not url and parser_id:
            self._ensure_boot()
            parser = self.protocol.parsers.get(parser_id, {})
            if parser and not parser.get("server", True):
                try:
                    url = self._external_play(parser_id, raw_id)
                except Exception:
                    url = ""
            if not url:
                try:
                    url = self.protocol.play(parser_id, raw_id)
                except Exception:
                    try:
                        self.protocol.reset_session()
                        self._ensure_boot()
                        url = self.protocol.play(parser_id, raw_id)
                    except Exception:
                        url = ""
        return {"parse": 0, "url": url or "", "header": self._play_header(url)}

    def _play_header(self, url: str) -> dict:
        # 多数第三方 CDN 带不带 Referer 都能播；但 picovr / ppvod / quark
        # 带 Referer 反而 403（实测）。动态：这些域名去 Referer。
        _no_ref = ("picovr.com", "ppvod", "drive.quark.cn")
        h = {"User-Agent": USER_AGENT}
        if not (url and any(d in url for d in _no_ref)):
            h["Referer"] = self.HOST + "/"
        return h

    def _is_playable_url(self, value) -> bool:
        if not isinstance(value, str):
            return False
        url = value.strip()
        if not re.match(r"^https?://", url, re.I):
            return False
        return any(m in url.lower() for m in
                   (".m3u8", ".mp4", ".mkv", ".flv", ".ts", "/m.php", "?data="))

    def _external_play(self, parser_id, play_id) -> str:
        import requests
        parser = self.protocol.parsers.get(parser_id, {})
        api_url = str(parser.get("url") or "").strip()
        if not api_url:
            return ""
        encoded = quote(str(play_id), safe="")
        target = api_url.replace("{url}", encoded) if "{url}" in api_url else api_url + encoded
        try:
            resp = requests.get(target, headers={"User-Agent": USER_AGENT}, timeout=15)
            payload = resp.json() if resp.status_code == 200 else {}
        except Exception:
            payload = {}
        return self._extract_play_url(payload, parser.get("result_key") or "url")

    def _extract_play_url(self, value, preferred_key="url") -> str:
        if isinstance(value, str):
            return value if self._is_playable_url(value) else ""
        if isinstance(value, dict):
            for key in [preferred_key, "url", "play_url", "playUrl", "m3u8", "data"]:
                if key in value:
                    found = self._extract_play_url(value[key], preferred_key)
                    if found:
                        return found
            for item in value.values():
                found = self._extract_play_url(item, preferred_key)
                if found:
                    return found
        elif isinstance(value, (list, tuple)):
            for item in value:
                found = self._extract_play_url(item, preferred_key)
                if found:
                    return found
        return ""

    # ── 诊断 ─────────────────────────────────────────────────────────

    def diag_urls(self) -> list[str]:
        return [
            self.HOST + "/app/bn/v2",
            self.HOST + "/app/bn/v2",
            self.HOST + "/app/bn/v2",
            self.HOST + "/app/bn/v2",
        ]