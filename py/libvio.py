#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LIBVIO 影视源 (dr_py / 苹果CMS)
站点: https://www.libvio.cam  (苹果CMS v10 + stui 模板, Cloudflare 前置)
框架: 该站列表/详情/搜索均为标准 HTML, 播放地址在 play 页的 player_aaaa.url
重要: 本站多数线路为网盘分享(夸克/百度), 能否播放取决于客户端是否支持网盘解析;
      playerContent 已自动判断: 直链(.m3u8/.mp4) -> parse=0 直接播, 其余 -> parse=1 交给客户端解析
生成依据: 已实网抓取并验证首页/分类/详情/搜索/play 五类页面结构
"""
import re
import json
import base64
from urllib.parse import quote, unquote

import requests
from base.spider import Spider


class MySpider(Spider):
    name = "LIBVIO"
    base_url = "https://www.libvio.cam"
    site_url = "https://www.libvio.cam"
    # 分类对应苹果CMS type id (来自站点首页菜单)
    class_name = ['电影', '电视剧', '纪录片', '动漫', '综艺', '动作', '喜剧', '爱情',
                  '科幻', '恐怖', '剧情', '战争', '国产剧', '港台剧', '日韩剧',
                  '海外剧', '国产动漫', '日韩动漫', '港台动漫', '欧美动漫']
    class_url = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12',
                 '13', '14', '15', '16', '24', '25', '26', '27']
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.libvio.cam/',
    }
    timeout = 20
    page_size = 24

    # ---------------- 内部工具 ----------------
    def _get(self, url, data=None):
        try:
            if data is None:
                resp = requests.get(url, headers=self.headers, timeout=self.timeout)
            else:
                resp = requests.post(url, headers=self.headers, data=data, timeout=self.timeout)
            resp.encoding = 'utf-8'
            return resp.text
        except Exception as e:
            print('[%s] 请求失败 %s: %s' % (self.name, url, e))
            return ''

    def _parse_list(self, html):
        """从列表/搜索页提取影片卡片 (每个 <li> 为一个影片)"""
        result = []
        for li in re.findall(r'<li>.*?</li>', html, re.S):
            href = re.search(r'href="(/detail/\d+\.html)"', li)
            title = re.search(r'title="([^"]*)"', li)
            pic = re.search(r'data-original="([^"]*)"', li)
            remark = re.search(r'pic-text[^>]*>([^<]*)<', li)
            if href and title:
                result.append({
                    'vod_id': href.group(1),
                    'vod_name': title.group(1).strip(),
                    'vod_pic': pic.group(1) if pic else '',
                    'vod_remarks': remark.group(1).strip() if remark else '',
                })
        return result

    def _pagecount(self, html, tid):
        """从分页器提取最大页码"""
        pages = re.findall(r'href="/type/%s-(\d+)\.html"' % re.escape(tid), html)
        try:
            return max([int(p) for p in pages] + [1])
        except Exception:
            return 1

    # ---------------- 五个核心方法 ----------------
    def homeContent(self, filter=False):
        html = self._get(self.base_url + '/')
        return {'list': self._parse_list(html)}

    def categoryContent(self, tid, pg, filter=False, content=None):
        pg = int(pg) if str(pg).isdigit() else 1
        if pg <= 1:
            url = '%s/type/%s.html' % (self.base_url, tid)
        else:
            url = '%s/type/%s-%s.html' % (self.base_url, tid, pg)
        html = self._get(url)
        videos = self._parse_list(html)
        return {
            'list': videos,
            'page': pg,
            'pagecount': self._pagecount(html, tid),
            'limit': self.page_size,
            'total': self.page_size * self._pagecount(html, tid),
        }

    def detailContent(self, ids):
        vid = ids if isinstance(ids, str) else (ids[0] if ids else '')
        if not vid:
            return []
        html = self._get(self.base_url + vid)
        # 基本信息
        name = re.search(r'<h1[^>]*class="title"[^>]*>([^<]+)</h1>', html)
        pic = re.search(r'data-original="([^"]+)"', html)
        content = re.search(r'detail-content[^>]*>([\s\S]*?)</(?:div|span)>', html)
        vod = {
            'vod_id': vid,
            'vod_name': name.group(1).strip() if name else '',
            'vod_pic': pic.group(1) if pic else '',
            'vod_content': re.sub(r'<[^>]+>', '', content.group(1)).strip() if content else '',
            'vod_play_from': '',
            'vod_play_url': '',
        }
        # 播放列表: /play/{id}-{sid}-{nid}.html , 按 sid 分组去重
        items = re.findall(
            r'<a\s+href="(/play/(\d+)-(\d+)-(\d+)\.html)"[^>]*>([^<]*)</a>', html)
        groups = {}
        seen = set()
        for href, _, sid, nid, title in items:
            if href in seen:
                continue
            seen.add(href)
            ep_name = title.strip() or ('第%s集' % nid)
            groups.setdefault(sid, []).append('%s$%s' % (ep_name, href))
        if groups:
            vod['vod_play_from'] = '$$$'.join('线路%s' % s for s in groups.keys())
            vod['vod_play_url'] = '$$$'.join('#'.join(eps) for eps in groups.values())
        return [vod]

    def searchContent(self, key, pg, filter=False):
        pg = int(pg) if str(pg).isdigit() else 1
        # 苹果CMS 搜索: POST /search/-------------.html  wd=关键词
        html = self._get(self.base_url + '/search/-------------.html',
                         data={'wd': key, 'pg': pg})
        if not html or 'search' not in html.lower():
            # 兜底: GET 伪静态 /search/{wd}.html
            html = self._get('%s/search/%s.html' % (self.base_url, quote(key)))
        videos = self._parse_list(html)
        # 搜索结果分页(取最大页码)
        pages = re.findall(r'href="/search/[^"]*-(\d+)\.html"', html)
        try:
            pc = max([int(p) for p in pages] + [1])
        except Exception:
            pc = 1
        return {
            'list': videos,
            'page': pg,
            'pagecount': pc,
            'limit': self.page_size,
            'total': self.page_size * pc,
        }

    def playerContent(self, flag, id, vipFlags=None):
        if not id.startswith('http'):
            id = self.base_url + id
        html = self._get(id)
        # 提取 player_aaaa 完整 JSON (括号匹配, 避免内部花括号提前截断)
        url = ''
        i = html.find('var player_aaaa')
        if i >= 0:
            j = html.find('{', i)
            depth = 0
            end = -1
            for k in range(j, len(html)):
                if html[k] == '{':
                    depth += 1
                elif html[k] == '}':
                    depth -= 1
                    if depth == 0:
                        end = k + 1
                        break
            if end > 0:
                try:
                    cfg = json.loads(html[j:end])
                    enc = str(cfg.get('encrypt', '0'))
                    url = (cfg.get('url', '') or '').replace('\\/', '/')
                    if enc == '1':
                        url = unquote(url)
                    elif enc == '2':
                        try:
                            url = unquote(base64.b64decode(url).decode('utf-8', 'ignore'))
                        except Exception:
                            pass
                except Exception as e:
                    print('[%s] player_aaaa 解析失败: %s' % (self.name, e))
        if not url:
            return {'parse': 1, 'url': id, 'header': {}}
        # 直链直接播, 网盘/其他交给客户端解析(parse=1)
        if url.endswith(('.m3u8', '.mp4', '.flv')):
            return {'parse': 0, 'url': url,
                    'header': {'User-Agent': self.headers['User-Agent']}}
        return {'parse': 1, 'url': url, 'header': {}}


if __name__ == '__main__':
    # 本地简单自测 (需 pip install requests)
    s = MySpider()
    print('--- 首页 ---')
    h = s.homeContent()
    print('首页影片数:', len(h.get('list', [])))
    if h.get('list'):
        print('示例:', h['list'][0])
    print('--- 电视剧第1页 ---')
    c = s.categoryContent('2', 1)
    print('分类影片数:', len(c.get('list', [])), 'pagecount:', c.get('pagecount'))
    print('--- 详情(取首页首条) ---')
    if h.get('list'):
        d = s.detailContent(h['list'][0]['vod_id'])
        if d:
            print('名称:', d[0]['vod_name'], '| 线路:', d[0]['vod_play_from'][:60])
    print('--- 搜索 狂 ---')
    r = s.searchContent('狂', 1)
    print('搜索结果数:', len(r.get('list', [])))
