# -*- coding: utf-8 -*-
"""
红果短剧 - OK影视 Python 点播源（封面优化版）
站点: https://www.hongguoapp.cn
"""

import json
import re
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
    host = "https://www.hongguoapp.cn"
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )
    # 有内容的栏目优先；空栏目仍保留入口
    GENRES = [
        "全部", "最新", "最热",
        "古装", "喜剧", "家庭", "犯罪", "奇幻", "剧情", "乡村",
        "战争", "动作", "历史", "商战", "网剧",
        "青春偶像", "经典", "情景", "其他",
    ]

    def init(self, extend=""):
        self.headers = {
            "User-Agent": self.UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self.host + "/",
            "Connection": "keep-alive",
        }
        self._pagecount = 50
        self.session = None
        try:
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry

            self.session = requests.Session()
            self.session.headers.update(self.headers)
            self.session.verify = False
            retry = Retry(
                total=2, connect=2, read=2, backoff_factor=0.25,
                status_forcelist=(502, 503, 504),
                allowed_methods=frozenset(["GET"]),
            )
            ad = HTTPAdapter(pool_connections=12, pool_maxsize=24, max_retries=retry)
            self.session.mount("https://", ad)
            self.session.mount("http://", ad)
        except Exception:
            self.session = None

    def getName(self):
        return "红果短剧"

    def isVideoFormat(self, url):
        u = (url or "").lower()
        return any(x in u for x in (".m3u8", ".mp4", ".flv", ".mkv"))

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

    def _abs(self, url):
        if not url:
            return ""
        url = str(url).strip().strip('"').strip("'")
        if not url or url.startswith("data:"):
            return ""
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = self.host + url
        elif not re.match(r"^https?://", url, re.I):
            url = self.host + "/" + url.lstrip("/")
        # http 提 https，减少客户端混合内容问题
        if url.startswith("http://"):
            url = "https://" + url[7:]
        return url

    def _pic(self, url):
        """只返回直链绝对地址（OK影视对第三方图床中转兼容差）"""
        url = self._abs(url)
        if not url:
            return ""
        # 去掉多余空格、反引号
        url = url.replace(" ", "").replace("`", "")
        return url

    def homeContent(self, filter):
        classes = [
            {"type_id": ("all" if n == "全部" else n), "type_name": n}
            for n in self.GENRES
        ]
        return {"class": classes, "filters": {}, "filterable": 0}

    def homeVideoContent(self):
        return {"list": self._list_page("最新", 1)[:18]}

    def categoryContent(self, tid, pg, filter, extend):
        pg = max(int(pg or 1), 1)
        tid = str(tid or "all")
        videos = self._list_page(tid, pg)
        pagecount = max(int(self._pagecount or 1), pg if videos else 1)
        if not videos and pg > 1:
            pagecount = max(pg - 1, 1)
        return {
            "list": videos,
            "page": pg,
            "pagecount": max(pagecount, 1),
            "limit": 36,
            "total": max(pagecount, 1) * 36,
        }

    def detailContent(self, ids):
        vid = str(ids[0] if isinstance(ids, list) else ids).strip()
        if not vid:
            return {"list": []}
        st, html = self._get(f"{self.host}/voddetail/{vid}.html")
        if st != 200 or not html:
            return {"list": []}

        title = self._strip(
            self._m(html, r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>([^<]+)')
            or self._m(html, r"<h1[^>]*>([^<]+)")
            or self._m(html, r"<h2[^>]*>([^<]+)")
            or f"短剧{vid}"
        )
        pic = self._pic(
            self._m(html, r'data-original="([^"]+)"')
            or self._m(html, r'property="og:image"\s+content="([^"]+)"')
            or self._m(html, r'(?:src|data-src)="([^"]*upload/vod[^"]+)"')
        )
        content = self._strip(
            self._m(
                html,
                r'class="[^"]*(?:hl-content-text|content|desc|vod_content)[^"]*"[^>]*>([\s\S]*?)</(?:div|span|p)>',
            )
            or title
        )
        remarks = self._strip(
            self._m(html, r'class="[^"]*(?:remarks|hl-text-remarks|pic-text)[^"]*"[^>]*>([^<]+)')
            or ""
        )

        eps, seen = [], set()
        for m in re.finditer(
            rf'href="(/vodplay/{re.escape(vid)}-(\d+)-(\d+)\.html)"[^>]*>([^<]*)',
            html,
        ):
            sid, ep, name = m.group(2), m.group(3), self._strip(m.group(4))
            key = f"{sid}-{ep}"
            if key in seen:
                continue
            seen.add(key)
            if not name or name in ("立即播放", "播放"):
                name = f"第{ep}集"
            eps.append(f"{name}${vid}-{sid}-{ep}")
        if not eps:
            for m in re.finditer(rf"/vodplay/{re.escape(vid)}-(\d+)-(\d+)\.html", html):
                key = f"{m.group(1)}-{m.group(2)}"
                if key in seen:
                    continue
                seen.add(key)
                eps.append(f"第{m.group(2)}集${vid}-{m.group(1)}-{m.group(2)}")
        if not eps:
            eps = [f"第1集${vid}-1-1"]

        by_sid = {}
        for item in eps:
            name, payload = item.split("$", 1)
            parts = payload.split("-")
            sid = parts[1] if len(parts) >= 3 else "1"
            by_sid.setdefault(sid, []).append(f"{name}${payload}")
        if len(by_sid) > 1:
            froms, urls = [], []
            for sid in sorted(by_sid.keys(), key=lambda x: int(x) if x.isdigit() else 0):
                froms.append(f"线路{sid}")
                urls.append("#".join(by_sid[sid]))
            play_from, play_url = "$$$".join(froms), "$$$".join(urls)
        else:
            play_from, play_url = "红果短剧", "#".join(eps)

        return {
            "list": [{
                "vod_id": vid,
                "vod_name": title,
                "vod_pic": pic,
                "vod_remarks": remarks,
                "vod_content": content,
                "vod_play_from": play_from,
                "vod_play_url": play_url,
            }]
        }

    def playerContent(self, flag, id, vipFlags):
        key = str(id or "").strip()
        play_url = ""
        if re.match(r"^\d+-\d+-\d+$", key):
            st, html = self._get(f"{self.host}/vodplay/{key}.html", timeout=10)
            if st == 200 and html:
                play_url = self._extract_play_url(html)
        elif key.isdigit():
            st, html = self._get(f"{self.host}/vodplay/{key}-1-1.html", timeout=10)
            if st == 200 and html:
                play_url = self._extract_play_url(html)
        elif key.startswith("http"):
            play_url = key
        return {
            "parse": 0 if play_url else 1,
            "jx": 0 if play_url else 1,
            "url": play_url or key,
            "header": {
                "User-Agent": self.UA,
                "Referer": self.host + "/",
                "Origin": self.host,
            },
        }

    def searchContent(self, key, quick, pg="1"):
        return self.searchContentPage(key, quick, pg)

    def searchContentPage(self, key, quick, pg="1"):
        pg = max(int(pg or 1), 1)
        wd = str(key or "").strip()
        if not wd:
            return {"list": [], "page": pg}
        q = quote(wd)
        # 搜索常被五秒盾，多路径尝试
        urls = [
            f"{self.host}/vodsearch/{q}----------{pg}---.html",
            f"{self.host}/index.php/vod/search/page/{pg}/wd/{q}.html",
            f"{self.host}/index.php/vod/search.html?wd={q}",
        ]
        for u in urls:
            st, html = self._get(u, timeout=10)
            if st == 200 and html and "Just a moment" not in html:
                videos = self._parse_list(html)
                if videos:
                    return {"list": videos, "page": pg}
        return {"list": [], "page": pg}

    def _list_url(self, tid, pg):
        pg = int(pg or 1)
        tid = str(tid or "all")
        if tid in ("最新", "time", "new"):
            return (
                f"{self.host}/vodshow/51--time---------.html"
                if pg <= 1
                else f"{self.host}/vodshow/51--time------{pg}---.html"
            )
        if tid in ("最热", "hits", "hot"):
            return (
                f"{self.host}/vodshow/51--hits---------.html"
                if pg <= 1
                else f"{self.host}/vodshow/51--hits------{pg}---.html"
            )
        if tid in ("all", "51", "", "全部"):
            return (
                f"{self.host}/vodshow/51-----------.html"
                if pg <= 1
                else f"{self.host}/vodshow/51--------{pg}---.html"
            )
        cls = quote(tid)
        return (
            f"{self.host}/vodshow/51---{cls}--------.html"
            if pg <= 1
            else f"{self.host}/vodshow/51---{cls}-----{pg}---.html"
        )

    def _list_page(self, tid, pg):
        st, html = self._get(self._list_url(tid, pg))
        if st != 200 or not html:
            return []
        self._pagecount = self._detect_pagecount(html, tid)
        return self._parse_list(html)

    def _detect_pagecount(self, html, tid):
        tid_s = str(tid or "")
        pages = []

        def add_nums(nums):
            for x in nums:
                try:
                    n = int(x)
                except Exception:
                    continue
                if 1 <= n <= 2000 and not (1900 <= n <= 2099):
                    pages.append(n)

        if tid_s in ("最新", "time", "new"):
            add_nums(re.findall(r"/vodshow/51--time------(\d+)---\.html", html))
        elif tid_s in ("最热", "hits", "hot"):
            add_nums(re.findall(r"/vodshow/51--hits------(\d+)---\.html", html))
        elif tid_s in ("all", "51", "", "全部"):
            add_nums(re.findall(r"/vodshow/51--------(\d+)---\.html", html))
        else:
            cls = quote(tid_s)
            add_nums(re.findall(rf"/vodshow/51---{re.escape(cls)}-----(\d+)---\.html", html))
            if not pages:
                add_nums(re.findall(r"/vodshow/51---[^\"']*?-----(\d+)---\.html", html))
        return max(pages) if pages else max(int(getattr(self, "_pagecount", 1) or 1), 1)

    def _parse_list(self, html):
        """按 li.hl-list-item 块解析，封面更稳"""
        out, seen = [], set()
        html = html or ""

        blocks = re.findall(
            r'<li[^>]*class="[^"]*hl-list-item[^"]*"[\s\S]*?</li>',
            html,
            flags=re.I,
        )
        if not blocks:
            # 兼容无 li 包裹
            blocks = re.findall(
                r'<a[^>]*class="[^"]*hl-item-thumb[^"]*"[\s\S]{0,500}?</a>',
                html,
                flags=re.I,
            )

        for block in blocks:
            vid = self._m(block, r"/voddetail/(\d+)\.html")
            if not vid or vid in seen:
                continue
            name = self._strip(
                self._m(block, r'title="([^"]+)"')
                or self._m(block, r'alt="([^"]+)"')
                or ""
            )
            pic = self._pic(
                self._m(block, r'data-original="([^"]+)"')
                or self._m(block, r'data-src="([^"]+)"')
                or self._m(block, r'src="([^"]*upload/vod[^"]+)"')
                or self._m(block, r'(?:src|data-src)="(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"')
            )
            remarks = self._strip(
                self._m(block, r'class="[^"]*(?:remarks|hl-lc-1|pic-text|hl-text-remarks)[^"]*"[^>]*>([^<]+)')
                or ""
            )
            if not name:
                name = f"短剧{vid}"
            seen.add(vid)
            out.append({
                "vod_id": vid,
                "vod_name": name,
                "vod_pic": pic,
                "vod_remarks": remarks,
            })

        # 全局正则兜底
        if not out:
            for m in re.finditer(
                r'href="(?:https?://[^"]+)?/voddetail/(\d+)\.html"\s+title="([^"]+)"\s+data-original="([^"]+)"',
                html,
            ):
                vid = m.group(1)
                if vid in seen:
                    continue
                seen.add(vid)
                out.append({
                    "vod_id": vid,
                    "vod_name": self._strip(m.group(2)),
                    "vod_pic": self._pic(m.group(3)),
                    "vod_remarks": "",
                })
        return out

    def _extract_play_url(self, html):
        html = html or ""
        m = re.search(r"player_aaaa\s*=\s*(\{[\s\S]*?\})\s*;?\s*</script>", html)
        if not m:
            m = re.search(r"var\s+player_[a-zA-Z0-9]+\s*=\s*(\{[\s\S]*?\})\s*;", html)
        if m:
            try:
                raw = re.sub(r",\s*}", "}", m.group(1))
                j = json.loads(raw)
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
            except Exception:
                pass
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
            ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
            ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'"),
        ):
            t = t.replace(a, b)
        return re.sub(r"\s+", " ", t).strip()
