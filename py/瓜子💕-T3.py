# -*- coding: utf-8 -*-
"""瓜子APP —— TVBox T4 源(逆向自瓜子系统请求协议, 2026-09)。

协议:
  · 域名轮询: apinew.uozvr.com / api.w32z7vtd.com / api.6a7nnf7.com /
      api.umygrx3.com / api.rmedphk.com (全挂→重新认证)
  · 设备认证: /App/Authentication/Device/signUp + /App/Authentication/Authenticator/refresh
      -> token / app_user_id
  · 业务: POST /App/...  body 为 urlencoded:
      request_key  = AES_CBC(JSON参数, key/iv).hex().upper()
      keys         = RSA_PKCS1v15(JSON{"iv","key"}).b64
      signature    = MD5(sign_str).upper()
      sign_str     = "token_id=,token={t},phone_type=1,request_key={rk},app_id=1,time={ts},keys={keys}*&zvdvdvddbfikkkumtmdwqppp?|4Y!s!2br"
  · 响应 data: {response_key, keys} -> RSA私钥解密keys拿resp key/iv -> AES解密response_key
  · 首页: /App/IndexList/indexList (tid/area/year/sort/page)
  · 详情: /App/IndexPlay/playInfo (vodInfo) + /App/Resource/Vurl/show (线路)
  · 搜索: /App/Index/findMoreVod
  · 播放: /App/Resource/VurlDetail/showOne (带 resolution)
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import string
import threading
import time
import urllib.parse

import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

from core.base import (
    VodDetailItem, VodHandler, VodItem, VodListResult, PlayerResult, HomeResult,
)
from core.cache import TTLCache

HOSTS = [
    "https://apinew.uozvr.com", "https://api.w32z7vtd.com",
    "https://api.6a7nnf7.com", "https://api.umygrx3.com", "https://api.rmedphk.com",
]
AES_KEY = "OITxa5OqAYjhswxx"
AES_IV = "rCMNwZASNBKZ8mXV"
RSA_PUB = ("MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDUM5+/y8sPsWkd1/RQS64X259EUwxFXFE5HlA65MqrxnPs0JqoSRojSDy5QhwvROlaD6TwRQHKMY2OAZ6SnQeUJsChTEFIR9qUkwrs3/MVUMxjsv6JS6Oe/juclyJGTgVmDhB55EafXsD0SQYVj/QXXsxR6ewR5E2kL52yAAD4yQIDAQAB")
RSA_PRIV = """-----BEGIN RSA PRIVATE KEY-----
MIICdgIBADANBgkqhkiG9w0BAQEFAASCAmAwggJcAgEAAoGAe6hKrWLi1zQmjTT1
ozbE4QdFeJGNxubxld6GrFGximxfMsMB6BpJhpcTouAqywAFppiKetUBBbXwYsYU
1wNr648XVmPmCMCy4rY8vdliFnbMUj086DU6Z+/oXBdWU3/b1G0DN3E9wULRSwcK
ZT3wj/cCI1vsCm3gj2R5SqkA9Y0CAwEAAQKBgAJH+4CxV0/zBVcLiBCHvSANm0l7
HetybTh/j2p0Y1sTXro4ALwAaCTUeqdBjWiLSo9lNwDHFyq8zX90+gNxa7c5EqcW
V9FmlVXr8VhfBzcZo1nXeNdXFT7tQ2yah/odtdcx+vRMSGJd1t/5k5bDd9wAvYdI
DblMAg+wiKKZ5KcdAkEA1cCakEN4NexkF5tHPRrR6XOY/XHfkqXxEhMqmNbB9U34
saTJnLWIHC8IXys6Qmzz30TtzCjuOqKRRy+FMM4TdwJBAJQZFPjsGC+RqcG5UvVM
iMPhnwe/bXEehShK86yJK/g/UiKrO87h3aEu5gcJqBygTq3BBBoH2md3pr/W+hUM
WBsCQQChfhTIrdDinKi6lRxrdBnn0Ohjg2cwuqK5zzU9p/N+S9x7Ck8wUI53DKm8
jUJE8WAG7WLj/oCOWEh+ic6NIwTdAkEAj0X8nhx6AXsgCYRql1klbqtVmL8+95KZ
K7PnLWG/IfjQUy3pPGoSaZ7fdquG8bq8oyf5+dzjE/oTXcByS+6XRQJAP/5ciy1b
L3NhUhsaOVy55MHXnPjdcTX0FaLi+ybXZIfIQ2P4rb19mVq1feMbCXhz+L1rG8oa
t5lYKfpe8k83ZA==
-----END RSA PRIVATE KEY-----"""
DEVICE_OLD_KEY = "aLFBMWpxBrIDAD1Si/KVvm41"
SIGN_SALT = "*&zvdvdvddbfikkkumtmdwqppp?|4Y!s!2br"
UA = "Lavf/57.83.100"

CATES = [("1", "电影"), ("2", "电视剧"), ("4", "动漫"), ("3", "综艺"), ("64", "短剧")]


class Guazi(VodHandler):
    source_name = "guazi"
    display_name = "瓜子影视"
    HOST = "https://apinew.uozvr.com"   # 域名锚点;实际轮询 HOSTS 列表

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache = TTLCache(1800)
        self._device_id = str(864150060000000 + random.randint(0, 9999))
        self._device_key = "".join(random.choices("0123456789ABCDEF", k=40))
        self._token = ""
        self._token_id = ""
        self._registered = False
        self._host_idx = 0
        self._rsa_pub = PKCS1_v1_5.new(RSA.import_key(
            "-----BEGIN PUBLIC KEY-----\n" + RSA_PUB + "\n-----END PUBLIC KEY-----"))
        self._rsa_priv = PKCS1_v1_5.new(RSA.import_key(RSA_PRIV))
        self._auth_ok = False
        self._init_auth()

    # ── 加解密 ────────────────────────────────────────────────────────

    def _aes_enc(self, text: str, key: str, iv: str) -> str:
        return AES.new(key.encode(), AES.MODE_CBC, iv.encode()).encrypt(
            pad(text.encode(), 16)).hex().upper()

    def _aes_dec(self, hexs: str, key: str, iv: str) -> str:
        raw = AES.new(key.encode(), AES.MODE_CBC, iv.encode()).decrypt(bytes.fromhex(hexs))
        return unpad(raw, 16).decode("utf-8")

    def _rsa_enc(self, text: str) -> str:
        return base64.b64encode(self._rsa_pub.encrypt(text.encode())).decode()

    def _rsa_dec(self, b64: str) -> str:
        raw = self._rsa_priv.decrypt(base64.b64decode(b64), None)
        return raw.decode("utf-8")

    @staticmethod
    def _md5(s: str) -> str:
        return hashlib.md5(s.encode()).hexdigest().upper()

    # ── 设备认证 ──────────────────────────────────────────────────────

    def _init_auth(self) -> None:
        try:
            self._sign_up()
            self._refresh()
            self._auth_ok = bool(self._token and self._token_id)
        except Exception:
            self._auth_ok = False

    def _sign_up(self) -> None:
        r = self._req("/App/Authentication/Device/signUp", {
            "new_key": self._device_key, "old_key": DEVICE_OLD_KEY,
            "phone_type": 1, "code": ""}, is_auth=True)
        self._apply(r)
        self._registered = True

    def _refresh(self) -> None:
        self._apply(self._req("/App/Authentication/Authenticator/refresh", {}, is_auth=True))

    def _apply(self, result) -> None:
        if not result:
            return
        if result.get("token"):
            self._token = result["token"]
        if result.get("app_user_id"):
            self._token_id = result["app_user_id"]

    def _ensure(self) -> None:
        if self._token and self._token_id:
            return
        if self._registered:
            r = self._req("/App/Authentication/Device/signIn", {
                "new_key": self._device_key, "old_key": DEVICE_OLD_KEY}, is_auth=True)
            self._apply(r)
        else:
            self._sign_up()
        self._refresh()

    # ── 加密请求核心 ──────────────────────────────────────────────────

    def _req(self, path: str, data: dict, is_auth: bool = False) -> dict:
        """加密请求 + 域名轮询。返回解密后的 dict(失败=None)。"""
        if not is_auth:
            self._ensure()
        params = json.dumps(data, ensure_ascii=False)
        request_key = self._aes_enc(params, AES_KEY, AES_IV)
        keys = self._rsa_enc(json.dumps({"iv": AES_IV, "key": AES_KEY}))
        t = str(int(time.time()))
        sign_str = (f"token_id=,token={self._token},phone_type=1,request_key={request_key},"
                    f"app_id=1,time={t},keys={keys}{SIGN_SALT}")
        body = {
            "token": self._token, "token_id": "", "phone_type": "1", "time": t,
            "phone_model": "xiaomi-25031", "keys": keys, "request_key": request_key,
            "signature": self._md5(sign_str), "app_id": "1", "ad_version": "1",
        }
        header = {
            "User-Agent": UA, "code": "GZ0369", "deviceId": self._device_id,
            "lang": "zh_cn", "Cache-Control": "no-cache",
            "Content-Type": "application/x-www-form-urlencoded",
            "Version": "2604028", "PackageName": "com.ae06aebdbb.y286327f5a.ofe849883320260517",
            "Ver": "3.0.3.2", "api-ver": "3.0.3.2",
        }
        tried = 0
        while tried < len(HOSTS):
            host = HOSTS[self._host_idx % len(HOSTS)]
            header["Referer"] = host
            try:
                r = requests.post(host + path, headers=header, data=body, timeout=12, verify=False)
                if r.status_code == 200:
                    j = r.json()
                    if j.get("code") == 200 and j.get("data"):
                        d = j["data"]
                        enc = d.get("response_key", "")
                        ekeys = d.get("keys", "")
                        if enc and ekeys:
                            ki = json.loads(self._rsa_dec(ekeys))
                            return json.loads(self._aes_dec(enc, ki["key"], ki["iv"]))
                    if not is_auth and j.get("code") != 200:
                        # token 失效 → 重新认证后重试
                        self._token = ""
                        self._ensure()
            except Exception:
                pass
            self._host_idx += 1
            tried += 1
        return None

    # ── 卡片 ──────────────────────────────────────────────────────────

    @staticmethod
    def _decode_ext(ext) -> dict:
        """兼容 dict/JSON字符串/URL查询串。"""
        if not ext:
            return {}
        if isinstance(ext, dict):
            return ext
        s = str(ext).strip()
        try:
            d = json.loads(s)
            return d if isinstance(d, dict) else {}
        except Exception:
            pass
        out = {}
        for part in s.replace(";", "&").split("&"):
            if "=" in part:
                k, _, v = part.partition("=")
                out[k.strip()] = urllib.parse.unquote(v.strip())
        return out

    @staticmethod
    def _card(it: dict) -> dict:
        cont = it.get("vod_continu", 0)
        remarks = "电影" if cont == 0 else f"更新至{cont}集"
        return VodItem(
            vod_id=f"{it.get('vod_id', '')}/{cont}",
            vod_name=it.get("vod_name", "").strip(),
            vod_pic=it.get("vod_pic", "") or "",
            vod_remarks=remarks,
        ).to_dict()

    # ── 五个契约方法 ──────────────────────────────────────────────────

    def home(self) -> dict:
        classes = [{"type_id": t, "type_name": n} for t, n in CATES]
        filters = {}
        for tid, _ in CATES:
            filters[tid] = [
                {"key": "area", "name": "地区", "value": [
                    {"n": "全部", "v": "0"}, {"n": "大陆", "v": "大陆"}, {"n": "香港", "v": "香港"},
                    {"n": "台湾", "v": "台湾"}, {"n": "美国", "v": "美国"}, {"n": "韩国", "v": "韩国"},
                    {"n": "日本", "v": "日本"}, {"n": "英国", "v": "英国"}, {"n": "法国", "v": "法国"},
                    {"n": "泰国", "v": "泰国"}, {"n": "印度", "v": "印度"}, {"n": "其他", "v": "其他"}]},
                {"key": "year", "name": "年份", "value": [
                    {"n": "全部", "v": "0"}] + [{"n": str(y), "v": str(y)} for y in range(2026, 2004, -1)]
                 + [{"n": "更早", "v": "2004"}]},
                {"key": "sort", "name": "排序", "value": [
                    {"n": "最新", "v": "d_id"}, {"n": "最热", "v": "d_hits"}, {"n": "推荐", "v": "d_score"}]},
            ]
        # 推荐列表: 电影分类首页 + 电视剧首页(合并去重)
        recs, seen = [], set()
        for tid in ("1", "2"):
            try:
                data = self._req("/App/IndexList/indexList", {
                    "area": "0", "year": "0", "pageSize": "18", "sort": "d_id",
                    "page": "1", "tid": tid})
                for i in (data or {}).get("list", []):
                    vid = str(i.get("vod_id", ""))
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)
                    c = self._card(i)
                    if c:
                        recs.append(c)
            except Exception:
                continue
        return HomeResult(classes=classes, filters=filters, list=recs[:36]).to_dict()

    def category(self, type_id: str, page: str, ext: str = "") -> dict:
        pg = self.safe_int(page)
        ex = self._decode_ext(ext)
        body = {"area": ex.get("area", "0"), "year": ex.get("year", "0"),
                "pageSize": "30", "sort": ex.get("sort", "d_id"),
                "page": str(pg), "tid": str(type_id)}
        data = self._req("/App/IndexList/indexList", body)
        vids = [c for c in (self._card(i) for i in (data or {}).get("list", [])) if c]
        return VodListResult(list=vids, page=pg, pagecount=9999, limit=30,
                             total=999999).to_dict()

    def search(self, keyword: str, page: str, ext: str = "") -> dict:
        pg = self.safe_int(page)
        data = self._req("/App/Index/findMoreVod", {
            "keywords": keyword, "order_val": "1", "page": str(pg)})
        vids = [c for c in (self._card(i) for i in (data or {}).get("list", [])) if c]
        return VodListResult(list=vids, page=pg, pagecount=9999, limit=30,
                             total=999999).to_dict()

    def detail(self, ids: str) -> dict:
        vod_id = str(ids).split("/")[0].split(",")[0].strip()
        t = str(int(time.time()))
        q = self._req("/App/IndexPlay/playInfo", {
            "token_id": self._token_id, "vod_id": vod_id,
            "mobile_time": t, "token": self._token})
        v = (q or {}).get("vodInfo")
        if not isinstance(v, dict):
            return {"list": []}
        vod = {
            "vod_id": vod_id,
            "vod_name": v.get("vod_name", ""),
            "vod_pic": v.get("vod_pic", "") or "",
            "vod_year": str(v.get("vod_year", "") or ""),
            "vod_actor": str(v.get("vod_actor", "") or ""),
            "vod_director": str(v.get("vod_director", "") or ""),
            "vod_content": str(v.get("vod_use_content", "") or "").strip(),
        }
        area = str(v.get("vod_area", "") or "")
        if area:
            vod["vod_remarks"] = area
        jd = self._req("/App/Resource/Vurl/show", {"vurl_cloud_id": "2", "vod_d_id": vod_id})
        play_list = []
        if jd and jd.get("list"):
            lst = jd["list"]
            single = len(lst) == 1
            for index, item in enumerate(lst):
                if "play" not in item:
                    continue
                names, params = [], []
                for key, value in item["play"].items():
                    pv = value.get("param", "") if isinstance(value, dict) else ""
                    if pv:
                        names.append(key)
                        params.append(pv)
                if params:
                    pn = vod.get("vod_name", "") if single else str(index + 1)
                    play_list.append(f"{pn}${params[-1]}||{'@'.join(names)}")
        if play_list:
            vod["vod_play_from"] = "瓜子影视"
            vod["vod_play_url"] = "#".join(play_list)
        return {"list": [VodDetailItem(**vod).to_dict()]}

    def player(self, flag: str, play_url: str) -> dict:
        hdr = {"User-Agent": UA, "Referer": "http://WJiZxLXA2.com/"}
        try:
            parts = str(play_url).split("||")
            if len(parts) < 2:
                return PlayerResult(parse=0, url="").to_dict()
            param_str = parts[0]
            resos = [r for r in parts[1].split("@") if r.isdigit()]
            params = {}
            for pair in param_str.split("&"):
                if "=" in pair:
                    k, _, v = pair.partition("=")
                    params[k] = v
            if resos:
                params["resolution"] = sorted(resos, key=int, reverse=True)[0]
                data = self._req("/App/Resource/VurlDetail/showOne", params)
                if data and data.get("url"):
                    return PlayerResult(url=data["url"], header=hdr).to_dict()
            return PlayerResult(parse=0, url="").to_dict()
        except Exception:
            return PlayerResult(parse=0, url="").to_dict()

    # ── 诊断 ──────────────────────────────────────────────────────────

    def diag_urls(self) -> list[tuple[str, str]]:
        return [
            ("认证", HOSTS[0] + "/App/Authentication/Authenticator/refresh"),
            ("首页列表", HOSTS[0] + "/App/IndexList/indexList"),
            ("搜索", HOSTS[0] + "/App/Index/findMoreVod"),
        ]
