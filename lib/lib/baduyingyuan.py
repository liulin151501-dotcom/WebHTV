# -*- coding: utf-8 -*-
"""
八度影院 / 八度影视
原域名 h.sdgtrl.com 已失效，改用 https://www.8dys.com
列表: /vtype/{id}.html  详情: /vdetail/{id}.html  播放: /vplay/{id}-{sid}-{nid}.html
"""
import re
import json
import base64
import sys
from urllib.parse import quote, unquote

sys.path.append("..")
try:
    from base.spider import Spider as BaseSpider
except Exception:
    class BaseSpider:
        def init(self, extend=""):
            pass


class Spider(BaseSpider):
    host = "https://www.8dys.com"
    UA = (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    )
    TYPES = [
        ("1", "电影"),
        ("2", "电视剧"),
        ("3", "综艺"),
        ("4", "动漫"),
        ("5", "纪录"),
    ]

    def init(self, extend=""):
        self.headers = {
            "User-Agent": self.UA,
            "Referer": self.host + "/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        self.session = None
        try:
            import requests
            from requests.adapters import HTTPAdapter

            self.session = requests.Session()
            self.session.headers.update(self.headers)
            self.session.verify = False
            ad = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=2)
            self.session.mount("https://", ad)
            self.session.mount("http://", ad)
        except Exception:
            self.session = None

    def getName(self):
        return "八度影院"

    def isVideoFormat(self, url):
        u = (url or "").lower()
        return any(x in u for x in (".m3u8", ".mp4", ".flv"))

    def manualVideoCheck(self):
        return False

    def destroy(self):
        try:
            if self.session:
                self.session.close()
        except Exception:
            pass

    def _get(self, url, timeout=12):
        try:
            if self.session:
                r = self.session.get(url, timeout=timeout)
                r.encoding = "utf-8"
                return r.status_code, r.text
            import urllib.request
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.status, resp.read().decode("utf-8", "ignore")
        except Exception:
            return 0, ""

    def _abs(self, u):
        if not u:
            return ""
        u = str(u).strip()
        if u.startswith("//"):
            return "https:" + u
        if u.startswith("/"):
            return self.host + u
        return u

    def _proxy_img(self, url):
        """封面中转：图床无 UA 会 403，经 localProxy 带 UA 拉取"""
        url = self._abs(url)
        if not url:
            return ""
        if "m=img" in url:
            return url
        try:
            b64 = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii")
            pb = ""
            try:
                pb = self.getProxyUrl(local=True)
            except Exception:
                try:
                    pb = self.getProxyUrl()
                except Exception:
                    pb = ""
            if not pb:
                return url
            sep = "&" if "?" in pb else "?"
            return f"{pb}{sep}m=img&u={b64}"
        except Exception:
            return url

    def homeContent(self, filter):
        classes = [{"type_id": tid, "type_name": name} for tid, name in self.TYPES]
        return {"class": classes, "filters": {}, "list": []}

    def homeVideoContent(self):
        st, html = self._get(self.host + "/")
        if st != 200 or not html:
            return {"list": []}
        return {"list": self._parse_list(html)[:24]}

    def categoryContent(self, tid, pg, filter, extend):
        pg = max(int(pg or 1), 1)
        tid = str(tid or "1").strip()
        if pg <= 1:
            url = f"{self.host}/vtype/{tid}.html"
        else:
            url = f"{self.host}/vtype/{tid}/page/{pg}.html"
        st, html = self._get(url)
        if st != 200 or not html:
            return {"list": [], "page": pg, "pagecount": pg, "limit": 30, "total": 0}
        items = self._parse_list(html)
        pages = [int(x) for x in re.findall(rf"/vtype/{re.escape(tid)}/page/(\d+)\.html", html)]
        pagecount = max(pages) if pages else (pg if items else max(pg - 1, 1))
        if items and pagecount < pg:
            pagecount = pg
        return {
            "list": items,
            "page": pg,
            "pagecount": max(pagecount, 1),
            "limit": 30,
            "total": max(pagecount, 1) * 30,
        }

    def detailContent(self, ids):
        vid = str(ids[0] if isinstance(ids, list) else ids).strip()
        if not vid:
            return {"list": []}
        if vid.startswith("http"):
            m = re.search(r"/vdetail/(\d+)", vid)
            vid = m.group(1) if m else vid
        st, html = self._get(f"{self.host}/vdetail/{vid}.html")
        if st != 200 or not html:
            return {"list": []}

        title = self._strip(
            self._m(html, r"<title>([^<]+)")
            or self._m(html, r"<h1[^>]*>([^<]+)")
            or f"影片{vid}"
        )
        for cut in ("全集免费播放", "在线观看", "-", "八度影视", "电视剧", "电影"):
            if cut in title:
                title = title.split(cut)[0].strip()
        title = title.strip(" -_|") or f"影片{vid}"

        pic = self._abs(
            self._m(html, r'data-original="([^"]+)"')
            or self._m(html, r'property="og:image"\s+content="([^"]+)"')
            or ""
        )
        content = self._strip(
            self._m(html, r'class="[^"]*(?:content|sketch|desc)[^"]*"[^>]*>([\s\S]*?)</(?:div|p|span)>')
            or self._m(html, r'name="description"\s+content="([^"]+)"')
            or ""
        )

        # 线路名: href="#playlistX">线路一
        tab_map = {}
        for href, name in re.findall(
            r'href="#(playlist\d+)"[^>]*>([^<]+)', html
        ):
            name = self._strip(name)
            if name and "同" not in name:
                tab_map[href] = name

        # 分集
        by_sid = {}
        seen = set()
        for m in re.finditer(
            rf'href="(/vplay/{re.escape(vid)}-(\d+)-(\d+)\.html)"[^>]*>([^<]*)',
            html,
        ):
            href, sid, nid, name = m.group(1), m.group(2), m.group(3), self._strip(m.group(4))
            key = f"{sid}-{nid}"
            if key in seen:
                continue
            # 过滤明显广告锚点
            if name.startswith("#"):
                continue
            seen.add(key)
            if not name or name in ("播放", "立即播放"):
                name = f"第{nid}集"
            by_sid.setdefault(sid, []).append(f"{name}${vid}-{sid}-{nid}")

        if not by_sid:
            by_sid["1"] = [f"第1集${vid}-1-1"]

        # 按页面线路 Tab 顺序输出
        froms, urls = [], []
        ordered_sids = []
        for m in re.finditer(r'href="#playlist(\d+)"[^>]*>([^<]+)', html):
            sid, label = m.group(1), self._strip(m.group(2))
            if sid in by_sid and sid not in ordered_sids and label and "同" not in label:
                ordered_sids.append(sid)
                froms.append(label)
                urls.append("#".join(by_sid[sid]))
        for sid in sorted(by_sid.keys(), key=lambda x: int(x) if x.isdigit() else 0):
            if sid in ordered_sids:
                continue
            froms.append(tab_map.get(f"playlist{sid}") or f"线路{sid}")
            urls.append("#".join(by_sid[sid]))

        return {
            "list": [
                {
                    "vod_id": vid,
                    "vod_name": title,
                    "vod_pic": self._proxy_img(pic),
                    "vod_content": content,
                    "vod_remarks": "",
                    "vod_play_from": "$$$".join(froms),
                    "vod_play_url": "$$$".join(urls),
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        key = str(id or "").strip()
        play_url = ""
        if re.match(r"^\d+-\d+-\d+$", key):
            st, html = self._get(f"{self.host}/vplay/{key}.html", timeout=10)
            if st == 200 and html:
                play_url = self._extract_play(html)
        elif key.isdigit():
            st, html = self._get(f"{self.host}/vplay/{key}-1-1.html", timeout=10)
            if st == 200 and html:
                play_url = self._extract_play(html)
        elif key.startswith("http"):
            play_url = key
        header = {
            "User-Agent": self.UA,
            "Referer": self.host + "/",
            "Origin": self.host,
        }
        return {
            "parse": 0 if play_url else 1,
            "jx": 0 if play_url else 1,
            "url": play_url or key,
            "header": header,
        }

    def searchContent(self, key, quick, pg="1"):
        return self.searchContentPage(key, quick, pg)

    def searchContentPage(self, key, quick, pg="1"):
        pg = max(int(pg or 1), 1)
        wd = str(key or "").strip()
        if not wd:
            return {"list": [], "page": pg}
        q = quote(wd)
        # MacCMS 搜索路径
        if pg <= 1:
            url = f"{self.host}/vsearch/{q}-------------.html"
        else:
            url = f"{self.host}/vsearch/{q}----------{pg}---.html"
        st, html = self._get(url)
        if st != 200 or not html:
            return {"list": [], "page": pg}
        return {"list": self._parse_list(html), "page": pg}

    def _parse_list(self, html):
        out, seen = [], []
        seen = set()
        html = html or ""
        # href="/vdetail/ID.html" ... title="NAME" data-original="PIC"
        for m in re.finditer(
            r'href="/vdetail/(\d+)\.html"[^>]*title="([^"]+)"[^>]*data-original="([^"]+)"',
            html,
        ):
            vid, name, pic = m.group(1), self._strip(m.group(2)), self._abs(m.group(3))
            if vid in seen:
                continue
            seen.add(vid)
            tail = html[m.end() : m.end() + 280]
            rem = re.search(r'class="[^"]*pic-text[^"]*"[^>]*>([^<]+)', tail)
            remarks = self._strip(rem.group(1)) if rem else ""
            out.append(
                {
                    "vod_id": vid,
                    "vod_name": name,
                    "vod_pic": self._proxy_img(pic),
                    "vod_remarks": remarks,
                }
            )
        if out:
            return out
        # 属性顺序不同
        for m in re.finditer(
            r'href="/vdetail/(\d+)\.html"[^>]*data-original="([^"]+)"[^>]*title="([^"]+)"',
            html,
        ):
            vid, pic, name = m.group(1), self._abs(m.group(2)), self._strip(m.group(3))
            if vid in seen:
                continue
            seen.add(vid)
            out.append(
                {
                    "vod_id": vid,
                    "vod_name": name,
                    "vod_pic": self._proxy_img(pic),
                    "vod_remarks": "",
                }
            )
        return out

    def localProxy(self, param):
        """图片本地代理：带 UA 拉取封面"""
        try:
            if isinstance(param, str):
                param = json.loads(param)
        except Exception:
            param = {}
        if not isinstance(param, dict):
            return [404, "text/plain", b""]
        if str(param.get("m", "")) != "img":
            return [404, "text/plain", b""]
        u = str(param.get("u") or "").strip()
        if not u:
            return [404, "text/plain", b""]
        try:
            pad = "=" * ((4 - len(u) % 4) % 4)
            raw = base64.urlsafe_b64decode(u + pad).decode("utf-8", "ignore")
        except Exception:
            return [404, "text/plain", b""]
        if not raw.startswith("http"):
            return [404, "text/plain", b""]
        try:
            import requests as req
            headers = {
                "User-Agent": getattr(self, "UA", "Mozilla/5.0"),
                "Referer": getattr(self, "host", "") + "/",
                "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
            }
            r = req.get(raw, headers=headers, timeout=15, verify=False)
            if int(r.status_code) != 200 or not r.content:
                return [404, "text/plain", b""]
            ct = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip() or "image/jpeg"
            return [200, ct, r.content]
        except Exception as e:
            print("img proxy error:", e)
            return [404, "text/plain", b""]


    def _extract_play(self, html):
        html = html or ""
        m = re.search(r"player_aaaa\s*=\s*(\{[\s\S]*?\})\s*;?\s*</script>", html)
        if not m:
            m = re.search(r"var\s+player_[a-zA-Z0-9]+\s*=\s*(\{[\s\S]*?\})\s*;", html)
        if m:
            raw = m.group(1)
            j = None
            try:
                j = json.loads(raw)
            except Exception:
                for i in range(len(raw), max(len(raw) - 200, 0), -1):
                    try:
                        j = json.loads(raw[:i])
                        break
                    except Exception:
                        pass
            if j:
                url = str(j.get("url") or "").replace("\\/", "/")
                enc = j.get("encrypt")
                if enc in (1, "1"):
                    url = unquote(url)
                elif enc in (2, "2"):
                    import base64

                    url = unquote(base64.b64decode(url).decode("utf-8", "ignore"))
                if url.startswith("//"):
                    url = "https:" + url
                if url.startswith("http"):
                    return url
        m = re.search(r'(https?://[^"\'\\]+\.m3u8[^"\'\\]*)', html)
        return m.group(1).replace("\\/", "/") if m else ""

    @staticmethod
    def _m(s, pat):
        m = re.search(pat, s or "", re.I)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _strip(t):
        t = re.sub(r"<[^>]+>", "", str(t or ""))
        for a, b in (
            ("&nbsp;", " "),
            ("&amp;", "&"),
            ("&lt;", "<"),
            ("&gt;", ">"),
            ("&quot;", '"'),
            ("&#39;", "'"),
        ):
            t = t.replace(a, b)
        return re.sub(r"\s+", " ", t).strip()
