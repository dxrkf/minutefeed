import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
import minutefeed as m

BASE='https://example.org/feed.xml'
def item(name, url=None, excerpt='brief text'):
    return {'title':name,'url':url or 'https://example.org/'+name,'excerpt':excerpt,'source':'Demo'}
def feed(items):
    return {'name':'Demo','url':BASE,'status':'ok','items':items,'skipped_entries':0}
def rss(items):
    return ('<rss version="2.0"><channel><title>Demo</title>'+items+'</channel></rss>').encode()

class Tests(unittest.TestCase):
    def test_text_cleanup(self):self.assertEqual(m.plain('<p>A &amp; B</p><script>bad()</script><p>C</p>',100),'A & B C')
    def test_excerpt_bound(self):self.assertEqual(len(m.plain('a'*300,240)),240)
    def test_public_https_validation(self):
        self.assertEqual(m.https_url(BASE),BASE)
        for url in ['http://example.org/a','https://u:p@example.org/a','https://localhost/a','https://127.0.0.1/a','https://example.org:444/a','https://example.org/a\nx']:
            with self.subTest(url=url),self.assertRaises(ValueError):m.https_url(url)
    def test_canonical_link(self):self.assertEqual(m.canonical_link('/post?id=3&utm_source=x&gclid=y#part',BASE),'https://example.org/post?id=3')
    def test_query_values_preserved(self):self.assertEqual(m.canonical_link('/p?q=a%20b&empty=',BASE),'https://example.org/p?q=a+b&empty=')
    def test_rss_parsing(self):
        r=m.parse_feed(rss('<item><title>A</title><description>&lt;p&gt;Text&lt;/p&gt;</description><link>/a</link></item>'),BASE)
        self.assertEqual(r['items'][0]['excerpt'],'Text');self.assertEqual(r['items'][0]['url'],'https://example.org/a')
    def test_atom_prefers_alternate(self):
        raw=b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title><entry><title>A</title><link rel="self" href="https://example.org/api"/><link href="/article"/><summary>Text</summary></entry></feed>'
        self.assertEqual(m.parse_feed(raw,BASE)['items'][0]['url'],'https://example.org/article')
    def test_missing_or_unsafe_entries_skipped(self):
        r=m.parse_feed(rss('<item><title>A</title></item><item><title>B</title><link>javascript:alert(1)</link></item>'),BASE)
        self.assertEqual(r['items'],[]);self.assertEqual(r['skipped_entries'],2)
    def test_dtd_rejected(self):
        with self.assertRaises(ValueError):m.parse_feed(b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss/>',BASE)
    def test_unsupported_xml_rejected(self):
        with self.assertRaises(ValueError):m.parse_feed(b'<html/>',BASE)
    def test_entry_limit_is_reported(self):
        r=m.parse_feed(rss('<item><title>A</title><link>/a</link></item>'*55),BASE)
        self.assertEqual(len(r['items']),50);self.assertEqual(r['entries_over_limit'],5)
    def test_round_robin_sources(self):
        r=m.digest([feed([item('a'),item('b')]),feed([item('c'),item('d')])])
        self.assertEqual([x['title'] for x in r['items']],['a','c','b','d'])
    def test_duplicates_removed(self):
        r=m.digest([feed([item('a'),item('b',url='https://example.org/a')])]);self.assertEqual(r['duplicates_removed'],1);self.assertEqual(len(r['items']),1)
    def test_budget_never_exceeded_and_shorter_later_fits(self):
        r=m.digest([feed([item('long',excerpt='word '*300),item('short')])],minutes=1,wpm=50)
        self.assertEqual([i['title'] for i in r['items']],['short']);self.assertEqual(r['skipped_budget'],1);self.assertLessEqual(r['estimated_seconds'],60)
    def test_item_limit(self):
        r=m.digest([feed([item('a'),item('b')])],max_items=1);self.assertEqual(r['skipped_item_limit'],1)
    def test_cjk_estimate_and_minimum(self):
        self.assertEqual(m.reading_seconds(item('字'*100,excerpt=''),100),60);self.assertEqual(m.reading_seconds(item('a',excerpt=''),200),5)
    def test_bad_settings(self):
        for args in [{'minutes':0},{'wpm':49},{'max_items':0}]:
            with self.assertRaises(ValueError):m.digest([],**args)
    def test_partial_feed_status(self):
        r=m.digest([feed([item('a')]),{'url':BASE,'status':'error'}]);self.assertTrue(r['partial']);self.assertEqual(len(r['items']),1)
    def test_fetch_and_response_cap(self):
        def fake(req,timeout):self.assertEqual(req.full_url,BASE);self.assertEqual(timeout,25);return io.BytesIO(b'<rss/>')
        self.assertEqual(m.fetch(BASE,opener=fake),b'<rss/>')
        with self.assertRaises(ValueError):m.fetch(BASE,opener=lambda *a,**k:io.BytesIO(b'x'*(m.MAX_BYTES+1)))
    def test_network_rate_limit_and_redirect_errors(self):
        for error in [urllib.error.URLError('offline'),urllib.error.HTTPError(BASE,429,'limit',{},None),urllib.error.HTTPError(BASE,301,'redirect',{},None)]:
            def fake(*a,**k):raise error
            with self.assertRaises(ValueError):m.fetch(BASE,opener=fake)
    def test_html_escapes_metadata(self):
        r=m.digest([feed([item('<script>bad</script>',excerpt='<img src=x>')])]);doc=m.render_html(r)
        self.assertNotIn('<script>',doc);self.assertIn('&lt;script&gt;',doc);self.assertNotIn('<img',doc)
    def test_cli_network_failure_reports_partial(self):
        with patch('sys.argv',['minutefeed',BASE,'--json']),patch('minutefeed.fetch',side_effect=ValueError('offline')),patch('sys.stdout',new_callable=io.StringIO) as out:
            self.assertEqual(m.main(),2);self.assertTrue(json.loads(out.getvalue())['partial'])
    def test_atomic_replace_and_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'out';m.atomic_write(p,'old');m.atomic_write(p,'new');self.assertEqual(p.read_text(),'new')
            link=Path(d)/'link';link.symlink_to(p)
            with self.assertRaises(ValueError):m.atomic_write(link,'bad')
            self.assertEqual(p.read_text(),'new')

if __name__=='__main__':unittest.main()
