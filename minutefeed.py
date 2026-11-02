#!/usr/bin/env python3
"""Build a time-budgeted digest from trusted public HTTPS RSS/Atom feeds."""
import argparse
import html
import ipaddress
import json
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from itertools import zip_longest
from pathlib import Path

MAX_BYTES = 1_000_000
ATOM = '{http://www.w3.org/2005/Atom}'
TRACKERS = {'fbclid', 'gclid', 'msclkid'}


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'br', 'div', 'li') and not self.hidden:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'div', 'li') and not self.hidden:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value, limit):
    parser = TextOnly()
    parser.feed(value)
    text = ' '.join(''.join(parser.parts).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def https_url(value):
    if not isinstance(value, str) or len(value) > 4000 or any(ord(c) < 33 for c in value):
        raise ValueError('invalid HTTPS URL')
    url = urllib.parse.urlsplit(value)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.port not in (None, 443):
        raise ValueError('only credential-free HTTPS URLs on port 443 are supported')
    host = url.hostname.lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local')) or '.' not in host:
        raise ValueError('use a public feed hostname')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError('private/reserved IP URLs are unsupported')
    return value


def canonical_link(value, base):
    value = urllib.parse.urljoin(base, value.strip())
    https_url(value)
    url = urllib.parse.urlsplit(value)
    query = urllib.parse.parse_qsl(url.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if not k.lower().startswith('utm_') and k.lower() not in TRACKERS]
    return urllib.parse.urlunsplit(('https', url.netloc.lower(), url.path or '/', urllib.parse.urlencode(query), ''))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(url, timeout=25, opener=None):
    https_url(url)
    req = urllib.request.Request(url, headers={'User-Agent': 'MinuteFeed/1.0', 'Accept': 'application/rss+xml, application/atom+xml, application/xml'})
    open_request = opener or urllib.request.build_opener(NoRedirect()).open
    try:
        with open_request(req, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('feed exceeds 1 MB response limit')
        return raw
    except urllib.error.HTTPError as exc:
        raise ValueError(f'HTTP {exc.code}; check the feed URL or retry later if rate-limited') from None
    except (urllib.error.URLError, OSError):
        raise ValueError('feed request failed or timed out') from None


def parse_feed(raw, url):
    if len(raw) > MAX_BYTES:
        raise ValueError('feed exceeds 1 MB')
    text = raw.decode('utf-8-sig')
    if re.search(r'<!\s*(DOCTYPE|ENTITY)\b', text, re.I):
        raise ValueError('DTD/entity declarations are unsupported')
    root = ET.fromstring(text)
    if root.tag == 'rss':
        channel = root.find('channel')
        if channel is None:
            raise ValueError('RSS channel missing')
        name = plain(channel.findtext('title', default=url), 120)
        nodes = channel.findall('item')
        atom = False
    elif root.tag == ATOM + 'feed':
        name = plain(root.findtext(ATOM + 'title', default=url), 120)
        nodes = root.findall(ATOM + 'entry')
        atom = True
    else:
        raise ValueError('only RSS 2.0 and Atom 1.0 feeds are supported')
    items = []
    skipped = 0
    for node in nodes[:50]:
        if atom:
            title_node = node.find(ATOM + 'title')
            summary_node = node.find(ATOM + 'summary')
            links = [link for link in node.findall(ATOM + 'link') if link.get('rel', 'alternate') == 'alternate' and link.get('type', 'text/html') in ('text/html', 'application/xhtml+xml')]
            link = links[0].get('href', '') if links else ''
            title = ''.join(title_node.itertext()) if title_node is not None else ''
            summary = ''.join(summary_node.itertext()) if summary_node is not None else ''
        else:
            title = node.findtext('title', default='')
            summary = node.findtext('description', default='')
            link = node.findtext('link', default='')
        title = plain(title, 180)
        if not title or not link.strip():
            skipped += 1
            continue
        try:
            link = canonical_link(link, url)
        except ValueError:
            skipped += 1
            continue
        items.append({'title': title, 'excerpt': plain(summary, 240), 'url': link, 'source': name or url})
    return {'name': name or url, 'url': url, 'status': 'ok', 'items': items, 'skipped_entries': skipped, 'entries_over_limit': max(0, len(nodes) - 50)}


def reading_seconds(item, words_per_minute):
    text = item['title'] + ' ' + item['excerpt']
    # Count CJK ideographs individually; remaining runs approximate words.
    cjk = re.findall(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]', text)
    rest = re.sub(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]', ' ', text)
    units = len(cjk) + len(re.findall(r'\w+', rest, re.UNICODE))
    return max(5, math.ceil(units * 60 / words_per_minute))


def digest(feeds, minutes=3, wpm=200, max_items=12):
    if type(minutes) is not int or not 1 <= minutes <= 30 or type(wpm) is not int or not 50 <= wpm <= 600 or type(max_items) is not int or not 1 <= max_items <= 50:
        raise ValueError('invalid budget, reading speed or item limit')
    selected = []
    seen = set()
    duplicates = 0
    over_budget = 0
    over_limit = 0
    used = 0
    # Preserve each source's order, taking at most one candidate per source per round.
    for group in zip_longest(*(f.get('items', []) for f in feeds)):
        for item in group:
            if item is None:
                continue
            if item['url'] in seen:
                duplicates += 1
                continue
            seen.add(item['url'])
            seconds = reading_seconds(item, wpm)
            if len(selected) >= max_items:
                over_limit += 1
            elif used + seconds > minutes * 60:
                over_budget += 1
            else:
                selected.append(dict(item, estimated_seconds=seconds))
                used += seconds
    return {'format': 'minutefeed-v1', 'generated_at_utc': datetime.now(timezone.utc).isoformat(), 'budget_seconds': minutes * 60, 'estimated_seconds': used, 'words_per_minute': wpm, 'duplicates_removed': duplicates, 'skipped_budget': over_budget, 'skipped_item_limit': over_limit, 'partial': any(f['status'] != 'ok' for f in feeds), 'feeds': [{k: v for k, v in f.items() if k != 'items'} for f in feeds], 'items': selected, 'interpretation': 'Reading estimate covers displayed titles/excerpts only, not linked articles. Excerpts are publisher text, not AI summaries.'}


def render_html(report):
    esc = lambda value: html.escape(str(value), quote=True)
    cards = []
    for item in report['items']:
        cards.append('<article><p class="source">' + esc(item['source']) + ' · ~' + str(item['estimated_seconds']) + 's</p><h2><a rel="noreferrer noopener" href="' + esc(item['url']) + '">' + esc(item['title']) + '</a></h2><p>' + esc(item['excerpt']) + '</p></article>')
    errors = ''.join('<li>' + esc(f['url']) + ': ' + esc(f.get('error', f['status'])) + '</li>' for f in report['feeds'] if f['status'] != 'ok')
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><title>MinuteFeed</title><style>body{font:18px system-ui;background:#101923;color:#ecf1f7;max-width:850px;padding:25px;margin:30px auto}h1,a{color:#7dd3fc}p{line-height:1.7}article{padding:20px;margin:18px 0;background:#1c2c3d;border-radius:16px}.source,small{color:#adc1d3}h2{font-size:23px}</style><h1>MinuteFeed</h1><p>Generated: ''' + esc(report['generated_at_utc']) + '</p><p>' + str(len(report['items'])) + ' items · estimated ' + str(report['estimated_seconds']) + '/' + str(report['budget_seconds']) + ' seconds · partial: ' + str(report['partial']) + '</p><p>' + esc(report['interpretation']) + '</p><ul>' + errors + '</ul>' + ''.join(cards) + '<p>Static digest. Follow source links for context and updates. Re-run to refresh.</p></html>'


def atomic_write(path, text):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('output symlinks are refused')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('feeds', nargs='+', help='1 to 5 trusted public HTTPS feed URLs')
    parser.add_argument('--minutes', type=int, default=3)
    parser.add_argument('--wpm', type=int, default=200)
    parser.add_argument('--max-items', type=int, default=12)
    parser.add_argument('--timeout', type=int, default=25)
    parser.add_argument('--html')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        if not 1 <= len(args.feeds) <= 5 or not 1 <= args.timeout <= 60:
            raise ValueError('use 1..5 feeds and timeout 1..60')
        digest([], args.minutes, args.wpm, args.max_items)
        urls = list(dict.fromkeys(https_url(u) for u in args.feeds))
        feeds = []
        for url in urls:
            try:
                feeds.append(parse_feed(fetch(url, args.timeout), url))
            except (ValueError, UnicodeError, ET.ParseError, OSError):
                feeds.append({'url': url, 'status': 'error', 'error': 'Feed unavailable, redirected, too large or unsupported XML; retry later or check URL.'})
        report = digest(feeds, args.minutes, args.wpm, args.max_items)
        if args.html:
            atomic_write(args.html, render_html(report))
        if args.json:
            print(json.dumps(report, ensure_ascii=True, indent=2))
        else:
            print(f"MinuteFeed: {len(report['items'])} items, ~{report['estimated_seconds']}/{report['budget_seconds']} seconds, partial={report['partial']}")
            for item in report['items']:
                print(f"[{item['source']}] {item['title']} (~{item['estimated_seconds']}s)\n{item['excerpt']}\n{item['url']}\n")
            print(report['interpretation'])
        return 2 if report['partial'] else 0
    except (ValueError, OSError):
        print('error: invalid settings or output path; no fresh digest produced', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
