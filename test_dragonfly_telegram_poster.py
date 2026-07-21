import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

SCRIPT = Path(__file__).with_name('dragonfly_telegram_poster.py')
spec = importlib.util.spec_from_file_location('poster', SCRIPT)
poster = importlib.util.module_from_spec(spec)
sys.modules['poster'] = poster
spec.loader.exec_module(poster)


def cfg():
    return poster.Config(
        dragonfly_token='df',
        dragonfly_user_id='2723',
        telegram_token=None,
        telegram_chat_id='@channel',
        db_path=Path(':memory:'),
        dry_run=True,
        feed_type='all',
        request_delay=0,
        send_delay=0,
        text_delay=0,
        photo_delay=0,
        album_delay=0,
        animation_delay=0,
        mixed_media_delay=0,
        media_item_delay=0,
        poll_interval=15,
        limit=20,
        max_attempts=poster.DEFAULT_MAX_ATTEMPTS,
        keep_sent=poster.DEFAULT_KEEP_SENT,
        log_file=None,
        upload_media=False,
        alert_chat_id=None,
        discussion_chat_id=None,
        cookie_file=None,
    )


def post(**overrides):
    base = {
        'post_id': 123,
        'author_name': 'Alice <A>',
        'author_link': 'alice_profile',
        'created_at': '2026-07-20T08:00:00',
        'description': 'hello',
        'photos': [],
        'audios': [],
        'is_repost': False,
    }
    base.update(overrides)
    return base


class CaptureTelegram:
    def __enter__(self):
        self.calls = []
        self.orig_tg_request = poster.tg_request
        self.orig_sleep = poster.time.sleep
        def fake_tg_request(cfg, method, payload):
            self.calls.append((method, payload))
            return {'ok': True}
        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        return self.calls
    def __exit__(self, *exc):
        poster.tg_request = self.orig_tg_request
        poster.time.sleep = self.orig_sleep


class DragonflyPosterTests(unittest.TestCase):
    def test_configure_active_account_can_pin_named_account_without_rewriting_active(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'accounts.json'
            data = {
                'active': 'main',
                'accounts': [
                    {'name': 'main', 'access_token': 'tok-main', 'enabled': True},
                    {'name': 'backup_2', 'access_token': 'tok-b2', 'enabled': True},
                ],
            }
            path.write_text(json.dumps(data), encoding='utf-8')
            c = cfg()
            c.accounts_file = str(path)
            c.account_name = 'backup_2'
            c.cookie_file = '/tmp/cookies.txt'

            poster.configure_active_account(c)

            self.assertEqual(c.dragonfly_token, 'tok-b2')
            self.assertEqual(c.account_name, 'backup_2')
            self.assertTrue(c.account_pinned)
            self.assertIsNone(c.cookie_file)
            self.assertEqual(json.loads(path.read_text())['active'], 'main')
            self.assertFalse(poster.switch_dragonfly_account(c, '401'))
            self.assertEqual(json.loads(path.read_text())['active'], 'main')

    def test_api_get_json_retries_transient_urlerror(self):
        calls = {'n': 0}
        orig_urlopen = poster.urllib.request.urlopen
        orig_sleep = poster.time.sleep

        class Resp:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout):
            calls['n'] += 1
            if calls['n'] == 1:
                raise urllib.error.URLError('ssl eof')
            return Resp()

        poster.urllib.request.urlopen = fake_urlopen
        poster.time.sleep = lambda *_: None
        try:
            data = poster.api_get_json('https://example.test/api', {}, retries=1)
        finally:
            poster.urllib.request.urlopen = orig_urlopen
            poster.time.sleep = orig_sleep

        self.assertEqual(data, {'ok': True})
        self.assertEqual(calls['n'], 2)

    def test_api_get_json_retries_http_429_with_longer_sleep(self):
        calls = {'n': 0}
        sleeps = []
        orig_urlopen = poster.urllib.request.urlopen
        orig_sleep = poster.time.sleep

        class Resp:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout):
            calls['n'] += 1
            if calls['n'] == 1:
                raise urllib.error.HTTPError(
                    req.full_url,
                    429,
                    'Too Many Requests',
                    {},
                    io.BytesIO(b'too many'),
                )
            return Resp()

        poster.urllib.request.urlopen = fake_urlopen
        poster.time.sleep = lambda s: sleeps.append(s)
        try:
            data = poster.api_get_json('https://example.test/api', {}, retries=1)
        finally:
            poster.urllib.request.urlopen = orig_urlopen
            poster.time.sleep = orig_sleep

        self.assertEqual(data, {'ok': True})
        self.assertEqual(calls['n'], 2)
        self.assertEqual(sleeps[0], poster.DEFAULT_API_429_BASE_SLEEP)

    def test_api_get_json_uses_adaptive_429_backoff(self):
        calls = {'n': 0}
        sleeps = []
        alerts = []
        orig_urlopen = poster.urllib.request.urlopen
        orig_sleep = poster.time.sleep

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self): return b'{"ok": true}'

        def fake_urlopen(req, timeout):
            calls['n'] += 1
            if calls['n'] <= 3:
                raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {}, io.BytesIO(b'too many'))
            return Resp()

        poster.urllib.request.urlopen = fake_urlopen
        poster.time.sleep = lambda s: sleeps.append(s)
        try:
            data = poster.api_get_json('https://example.test/api', {}, retries=3, alert=alerts.append)
        finally:
            poster.urllib.request.urlopen = orig_urlopen
            poster.time.sleep = orig_sleep

        self.assertEqual(data, {'ok': True})
        self.assertEqual(sleeps, [30.0, 60.0, 120.0])
        self.assertIn('жду 30 сек', alerts[0])
        self.assertIn('жду 1 мин', alerts[1])
        self.assertIn('жду 2 мин', alerts[2])

    def test_api_get_json_sends_friendly_429_alert_callback(self):
        calls = {'n': 0}
        alerts = []
        orig_urlopen = poster.urllib.request.urlopen
        orig_sleep = poster.time.sleep

        class Resp:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout):
            calls['n'] += 1
            if calls['n'] == 1:
                raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {}, io.BytesIO(b'too many'))
            return Resp()

        poster.urllib.request.urlopen = fake_urlopen
        poster.time.sleep = lambda *_: None
        try:
            poster.api_get_json('https://example.test/api/feed?type=all&limit=20&offset=120', {}, retries=1, alert=alerts.append)
        finally:
            poster.urllib.request.urlopen = orig_urlopen
            poster.time.sleep = orig_sleep

        self.assertEqual(len(alerts), 1)
        self.assertIn('429', alerts[0])
        self.assertIn('жду 30 сек', alerts[0])
        self.assertIn('попытка 1/1', alerts[0])

    def test_dragonfly_headers_use_bearer_without_cookie_file(self):
        c = cfg()
        c.dragonfly_token = 'jwt-token'
        c.cookie_file = None

        headers = poster.dragonfly_headers(c)

        self.assertEqual(headers['Authorization'], 'Bearer jwt-token')
        self.assertIn('User-Agent', headers)

    def test_dragonfly_headers_omit_bearer_when_cookie_file_exists(self):
        c = cfg()
        c.dragonfly_token = 'jwt-token'
        c.cookie_file = '/tmp/dragonfly-cookies.txt'

        headers = poster.dragonfly_headers(c)

        self.assertNotIn('Authorization', headers)
        self.assertEqual(headers['Accept'], 'application/json')

    def test_api_get_json_sends_friendly_401_alert_before_raising(self):
        alerts = []
        orig_urlopen = poster.urllib.request.urlopen

        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'{"detail":"expired"}'))

        poster.urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(RuntimeError):
                poster.api_get_json('https://example.test/api/feed', {}, retries=0, auth_alert=alerts.append)
        finally:
            poster.urllib.request.urlopen = orig_urlopen

        self.assertEqual(len(alerts), 1)
        self.assertIn('Авторизация Dragonfly истекла', alerts[0])
        self.assertIn('401', alerts[0])

    def test_accounts_file_switches_to_next_account_on_401(self):
        c = cfg()
        c.dragonfly_token = None
        c.cookie_file = None
        with tempfile.TemporaryDirectory() as td:
            accounts_path = Path(td) / 'accounts.json'
            accounts_path.write_text(json.dumps({
                'active': 'main',
                'accounts': [
                    {'name': 'main', 'access_token': 'bad-token', 'enabled': True},
                    {'name': 'backup', 'access_token': 'good-token', 'enabled': True},
                ],
            }), encoding='utf-8')
            c.accounts_file = str(accounts_path)
            poster.configure_active_account(c)
            seen_auth = []
            orig_urlopen = poster.urllib.request.urlopen

            class Resp:
                def __enter__(self): return self
                def __exit__(self, *exc): return False
                def read(self): return b'{"feed": [{"post_id": 1}]}'

            def fake_urlopen(req, timeout):
                seen_auth.append(req.get_header('Authorization'))
                if 'bad-token' in (seen_auth[-1] or ''):
                    raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO(b'expired'))
                return Resp()

            poster.urllib.request.urlopen = fake_urlopen
            try:
                posts = poster.fetch_feed_page(c, 0)
            finally:
                poster.urllib.request.urlopen = orig_urlopen

            self.assertEqual([p['post_id'] for p in posts], [1])
            self.assertEqual(c.account_name, 'backup')
            self.assertIn('bad-token', seen_auth[0])
            self.assertIn('good-token', seen_auth[1])
            saved = json.loads(accounts_path.read_text(encoding='utf-8'))
            self.assertEqual(saved['active'], 'backup')

    def test_accounts_file_does_not_switch_on_429(self):
        c = cfg()
        with tempfile.TemporaryDirectory() as td:
            accounts_path = Path(td) / 'accounts.json'
            accounts_path.write_text(json.dumps({
                'active': 'main',
                'accounts': [
                    {'name': 'main', 'access_token': 'main-token', 'enabled': True},
                    {'name': 'backup', 'access_token': 'backup-token', 'enabled': True},
                ],
            }), encoding='utf-8')
            c.accounts_file = str(accounts_path)
            c.cookie_file = None
            poster.configure_active_account(c)
            orig_urlopen = poster.urllib.request.urlopen
            orig_sleep = poster.time.sleep

            def fake_urlopen(req, timeout):
                raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {}, io.BytesIO(b'too many'))

            poster.urllib.request.urlopen = fake_urlopen
            poster.time.sleep = lambda *_: None
            try:
                with self.assertRaises(RuntimeError):
                    poster.fetch_feed_page(c, 0)
            finally:
                poster.urllib.request.urlopen = orig_urlopen
                poster.time.sleep = orig_sleep

            self.assertEqual(c.account_name, 'main')
            saved = json.loads(accounts_path.read_text(encoding='utf-8'))
            self.assertEqual(saved['active'], 'main')

    def test_cmd_auth_check_returns_zero_when_feed_page_loads(self):
        c = cfg()
        calls = []
        orig = poster.fetch_feed_page
        poster.fetch_feed_page = lambda cfg, offset: calls.append(offset) or [post(post_id=1)]
        try:
            rc = poster.cmd_auth_check(c)
        finally:
            poster.fetch_feed_page = orig

        self.assertEqual(rc, 0)
        self.assertEqual(calls, [0])

    def test_send_alert_uses_alert_chat_id_and_html(self):
        c = cfg()
        c.telegram_token = 'bot-token'
        c.alert_chat_id = '123456789'
        calls = []
        orig = poster.tg_request
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True}
        try:
            poster.send_alert(c, '⚠️ Тест', 'получили <429> & ждём')
        finally:
            poster.tg_request = orig

        self.assertEqual(calls[0][0], 'sendMessage')
        self.assertEqual(calls[0][1]['chat_id'], '123456789')
        self.assertIn('⚠️ Тест', calls[0][1]['text'])
        self.assertIn('&lt;429&gt; &amp;', calls[0][1]['text'])

    def test_download_media_retries_transient_urlerror(self):
        calls = {'n': 0}
        sleeps = []
        orig_urlopen = poster.urllib.request.urlopen
        orig_sleep = poster.time.sleep

        class Resp:
            headers = {'content-type': 'image/jpeg', 'content-length': '4'}
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self, n=-1):
                return b'JPEG'

        def fake_urlopen(req, timeout):
            calls['n'] += 1
            if calls['n'] == 1:
                raise urllib.error.URLError('ssl eof')
            return Resp()

        poster.urllib.request.urlopen = fake_urlopen
        poster.time.sleep = lambda s: sleeps.append(s)
        try:
            filename, data, ctype = poster.download_media_bytes('https://dragonfly-flash.ru/photousers/x.jpg', retries=1)
        finally:
            poster.urllib.request.urlopen = orig_urlopen
            poster.time.sleep = orig_sleep

        self.assertEqual(filename, 'x.jpg')
        self.assertEqual(data, b'JPEG')
        self.assertEqual(ctype, 'image/jpeg')
        self.assertEqual(calls['n'], 2)
        self.assertTrue(sleeps)

    def test_header_has_linked_author_and_datetime_with_light_emoji(self):
        chunks = poster.format_html_chunks(post(description='hello'), limit=poster.MAX_TG_MESSAGE)

        self.assertIn('👤 <a href="https://dragonfly-flash.ru/?id=alice_profile">Alice &lt;A&gt;</a>', chunks[0])
        self.assertIn('🕒 <i>20.07.2026 12:00</i>', chunks[0])

    def test_text_keeps_apostrophes_and_combining_symbols_but_escapes_html(self):
        chunks = poster.format_html_chunks(post(description="Брад (ง'̀-'́)ง <script>&"), limit=poster.MAX_TG_MESSAGE)
        text = chunks[0]

        self.assertIn("(ง'̀-'́)ง", text)
        self.assertNotIn('&#x27;', text)
        self.assertIn('&lt;script&gt;&amp;', text)

    def test_text_decodes_existing_html_entities_before_safe_escape(self):
        chunks = poster.format_html_chunks(post(description="Брад (ง&#x27;̀-&#x27;́)ง &lt;b&gt;"), limit=poster.MAX_TG_MESSAGE)
        text = chunks[0]

        self.assertIn("(ง'̀-'́)ง", text)
        self.assertNotIn('&#x27;', text)
        self.assertIn('&lt;b&gt;', text)

    def test_log_file_receives_timestamped_lines(self):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        poster.setup_logging(path)
        try:
            poster.log('hello log file')
            text = Path(path).read_text(encoding='utf-8')
        finally:
            poster.setup_logging(None)
        self.assertIn('hello log file', text)
        self.assertIn('INFO', text)

    def test_empty_post_without_text_or_media_is_not_publishable(self):
        self.assertFalse(poster.is_publishable(post(description='   ', photos=[])))
        self.assertTrue(poster.is_publishable(post(description='text', photos=[])))
        self.assertTrue(poster.is_publishable(post(description='   ', photos=[{'url': '/x.jpg'}])))

    def test_audio_only_post_is_publishable_as_track_info(self):
        p = post(description='', photos=[], audios=[{'artist': 'Импакт', 'title': 'Социопат', 'file_path': 'audio.mp3'}])

        self.assertTrue(poster.is_publishable(p))
        chunks = poster.format_html_chunks(p, limit=poster.MAX_TG_MESSAGE)
        self.assertIn('🎵 <b>Трек:</b> Импакт — Социопат', '\n'.join(chunks))

    def test_long_text_post_is_split_into_bounded_messages_with_continuation(self):
        long_text = 'абвгд ' * 900
        with CaptureTelegram() as calls:
            poster.send_post(cfg(), post(description=long_text, photos=[]))

        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(method == 'sendMessage' for method, _ in calls))
        self.assertTrue(all(len(payload['text']) <= poster.MAX_TG_MESSAGE for _, payload in calls))
        self.assertIn('(1/', calls[0][1]['text'])
        self.assertIn('(2/', calls[1][1]['text'])
        self.assertIn('>#123</a>', calls[-1][1]['text'])
        self.assertNotIn('Открыть пост', calls[-1][1]['text'])

    def test_long_media_caption_is_split_caption_then_followup_messages(self):
        long_text = 'длинный текст ' * 240
        with CaptureTelegram() as calls:
            poster.send_post(cfg(), post(description=long_text, photos=[{'url': '/p.jpg'}]))

        self.assertEqual(calls[0][0], 'sendPhoto')
        self.assertLessEqual(len(calls[0][1]['caption']), poster.MAX_TG_CAPTION)
        self.assertIn('(1/', calls[0][1]['caption'])
        self.assertGreaterEqual(len(calls), 2)
        for _method, payload in calls:
            body = payload.get('text', payload.get('caption', ''))
            limit = poster.MAX_TG_CAPTION if 'caption' in payload else poster.MAX_TG_MESSAGE
            self.assertLessEqual(len(body), limit)
        self.assertEqual(calls[-1][0], 'sendMessage')
        self.assertIn('>#123</a>', calls[-1][1]['text'])
        self.assertNotIn('Открыть пост', calls[-1][1]['text'])

    def test_photo_count_is_not_rendered_in_caption(self):
        text = poster.format_html_chunks(post(description='hello', photos=[{'url': '/p.jpg'}]), limit=poster.MAX_TG_CAPTION)[0]

        self.assertNotIn('📷 1', text)

    def test_audio_info_is_rendered_and_counted_inside_caption_limit(self):
        p = post(
            description='hello ' * 220,
            photos=[{'url': '/p.jpg'}],
            audios=[{
                'artist': 'Massive Attack',
                'title': 'Angel',
                'duration': 379,
                'file_path': 'music/massive_attack_angel.mp3',
            }],
        )

        chunks = poster.format_html_chunks(p, limit=poster.MAX_TG_CAPTION)

        self.assertTrue(all(len(c) <= poster.MAX_TG_CAPTION for c in chunks))
        joined = '\n'.join(chunks)
        self.assertIn('🎵', joined)
        self.assertIn('Massive Attack — Angel', joined)
        self.assertIn('6:19', joined)
        self.assertNotIn('massive_attack_angel.mp3', joined)
        self.assertNotIn('audio_', joined)

    def test_dry_run_does_not_mark_posts_sent(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = True
        with CaptureTelegram():
            processed = poster.send_new_posts(c, con, [post(post_id=555, description='hello')])

        self.assertEqual(processed, 1)
        self.assertFalse(poster.is_sent(con, 555))

    def test_post_delay_depends_on_post_media_type(self):
        c = cfg()
        c.text_delay = 1
        c.photo_delay = 2
        c.album_delay = 3
        c.animation_delay = 4
        c.mixed_media_delay = 5

        self.assertEqual(poster.delay_for_post(c, post(description='text', photos=[])), 1)
        self.assertEqual(poster.delay_for_post(c, post(photos=[{'url': '/x.jpg'}])), 2)
        self.assertEqual(poster.delay_for_post(c, post(photos=[{'url': '/1.jpg'}, {'url': '/2.jpg'}])), 3)
        self.assertEqual(poster.delay_for_post(c, post(photos=[{'url': '/x.gif'}])), 4)
        self.assertEqual(poster.delay_for_post(c, post(photos=[{'url': '/x.gif'}, {'url': '/y.jpg'}])), 5)

    def test_send_new_posts_uses_type_specific_delay(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.text_delay = 1
        c.photo_delay = 2
        c.animation_delay = 4
        sleeps = []
        sent = []
        orig_send = poster.send_post
        orig_sleep = poster.time.sleep

        def fake_send(cfg, p, **kwargs):
            sent.append(int(p['post_id']))

        poster.send_post = fake_send
        poster.time.sleep = lambda s: sleeps.append(s)
        try:
            processed = poster.send_new_posts(c, con, [
                post(post_id=3, description='gif', photos=[{'url': '/x.gif'}]),
                post(post_id=2, description='photo', photos=[{'url': '/x.jpg'}]),
                post(post_id=1, description='text', photos=[]),
            ])
        finally:
            poster.send_post = orig_send
            poster.time.sleep = orig_sleep

        self.assertEqual(processed, 3)
        self.assertEqual(sent, [1, 2, 3])
        self.assertEqual(sleeps, [1, 2, 4])

    def test_catch_up_gaps_fetches_missing_id_and_sends_existing_post(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        seen_fetches = []
        sent = []
        orig_fetch = poster.fetch_post_by_id
        orig_send = poster.send_post
        orig_sleep = poster.time.sleep

        def fake_fetch(cfg, pid):
            seen_fetches.append(pid)
            return post(post_id=pid, description=f'missing {pid}') if pid == 102 else None

        def fake_send(cfg, p, **kwargs):
            sent.append(int(p['post_id']))

        poster.fetch_post_by_id = fake_fetch
        poster.send_post = fake_send
        poster.time.sleep = lambda *_: None
        try:
            processed = poster.catch_up_missing_ids(c, con, min_id=100, max_id=104, known_ids={100, 101, 103, 104})
        finally:
            poster.fetch_post_by_id = orig_fetch
            poster.send_post = orig_send
            poster.time.sleep = orig_sleep

        self.assertEqual(seen_fetches, [102])
        self.assertEqual(sent, [102])
        self.assertEqual(processed, 1)
        self.assertTrue(poster.is_sent(con, 102))

    def test_media_failure_falls_back_to_text_with_original_link(self):
        calls = []
        orig = poster.tg_request
        orig_sleep = poster.time.sleep
        def fake_tg_request(cfg, method, payload):
            calls.append((method, payload))
            if method in ('sendPhoto', 'sendAnimation', 'sendMediaGroup'):
                raise RuntimeError('bad media')
            return {'ok': True}
        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        try:
            poster.send_post(cfg(), post(description='text', photos=[{'url': '/bad.jpg'}]))
        finally:
            poster.tg_request = orig
            poster.time.sleep = orig_sleep

        self.assertEqual(calls[-1][0], 'sendMessage')
        self.assertIn('⚠️ Медиа не отправилось', calls[-1][1]['text'])
        self.assertIn('>#123</a>', calls[-1][1]['text'])
        self.assertNotIn('Открыть пост', calls[-1][1]['text'])

    def test_media_fallback_does_not_dump_rendered_html(self):
        calls = []
        orig = poster.tg_request
        orig_sleep = poster.time.sleep
        def fake_tg_request(cfg, method, payload):
            calls.append((method, payload))
            if method in ('sendPhoto', 'sendAnimation', 'sendMediaGroup'):
                raise RuntimeError('bad media')
            return {'ok': True}
        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        rendered = '<div class="post-header"><img src="/x.jpg"><div class="post-footer">Мне нравится 1</div></div>'
        try:
            poster.send_post(cfg(), post(post_id=24974, description=rendered, photos=[{'url': '/bad.jpg'}]))
        finally:
            poster.tg_request = orig
            poster.time.sleep = orig_sleep

        body = calls[-1][1]['text']
        self.assertIn('⚠️ Медиа не отправилось', body)
        self.assertIn('>#24974</a>', body)
        self.assertNotIn('post-header', body)
        self.assertNotIn('&lt;div', body)

    def test_send_new_posts_retries_media_before_text_fallback(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.max_attempts = 3
        calls = []
        orig = poster.tg_request
        orig_sleep = poster.time.sleep
        def fake_tg_request(cfg, method, payload):
            calls.append((method, payload))
            if method in ('sendPhoto', 'sendAnimation', 'sendMediaGroup'):
                raise RuntimeError('bad media')
            return {'ok': True, 'result': {'message_id': 700}}
        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        try:
            for _ in range(2):
                poster.send_new_posts(c, con, [post(post_id=903, description='text', photos=[{'url': '/bad.jpg'}])])
            self.assertFalse(poster.is_sent(con, 903))
            self.assertEqual(poster.failed_attempts(con, 903), 2)
            poster.send_new_posts(c, con, [post(post_id=903, description='text', photos=[{'url': '/bad.jpg'}])])
        finally:
            poster.tg_request = orig
            poster.time.sleep = orig_sleep

        self.assertTrue(poster.is_sent(con, 903))
        self.assertTrue(any(method == 'sendMessage' and '⚠️ Медиа не отправилось' in payload.get('text', '') for method, payload in calls))

    def test_send_new_posts_records_telegram_message_mapping(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        calls = []
        orig = poster.tg_request
        orig_sleep = poster.time.sleep

        def fake_tg_request(cfg, method, payload):
            calls.append((method, payload))
            return {'ok': True, 'result': {'message_id': 4242}}

        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        try:
            n = poster.send_new_posts(c, con, [post(post_id=888, description='hello', likes_count=2, comments_count=1)])
        finally:
            poster.tg_request = orig
            poster.time.sleep = orig_sleep

        self.assertEqual(n, 1)
        row = con.execute("SELECT message_id, message_kind, last_likes, last_comments FROM telegram_messages WHERE post_id=888 AND role='main'").fetchone()
        self.assertEqual(row, (4242, 'text', 2, 1))
        self.assertEqual(calls[0][0], 'sendMessage')
        self.assertIn('❤️ 2', calls[0][1]['text'])
        self.assertIn('💬 1', calls[0][1]['text'])
        stored = con.execute("SELECT base_html FROM telegram_messages WHERE post_id=888 AND role='main'").fetchone()[0]
        self.assertNotIn('❤️ 2', stored)

    def test_initial_zero_stats_footer_is_sent(self):
        with CaptureTelegram() as calls:
            poster.send_post(cfg(), post(description='hello', likes_count=0, comments_count=0))

        self.assertIn('❤️ 0', calls[0][1]['text'])
        self.assertIn('💬 0', calls[0][1]['text'])

    def test_discussion_mapping_detects_automatic_forward_update(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        c.discussion_chat_id = '-100222'
        updates = [{
            'update_id': 10,
            'message': {
                'message_id': 777,
                'chat': {'id': -100222},
                'is_automatic_forward': True,
                'forward_from_chat': {'id': -100111},
                'forward_from_message_id': 555,
            },
        }]
        orig = poster.tg_request
        poster.tg_request = lambda cfg, method, payload: {'ok': True, 'result': updates}
        try:
            did = poster.try_capture_discussion_mapping(c, con, post_id=901, role='last', channel_message_id=555, wait_seconds=0)
        finally:
            poster.tg_request = orig

        self.assertEqual(did, 777)
        row = con.execute("SELECT discussion_chat_id, discussion_message_id FROM telegram_discussion_messages WHERE post_id=901 AND role='last'").fetchone()
        self.assertEqual(row, ('-100222', 777))
        self.assertEqual(poster.kv_get(con, 'telegram_updates_offset'), '11')

    def test_sync_comments_seeds_existing_then_sends_new_comment(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        poster.save_discussion_message(
            con,
            post_id=902,
            role='last',
            channel_chat_id='@channel',
            channel_message_id=555,
            discussion_chat_id='-100222',
            discussion_message_id=777,
        )
        comments_round_1 = [{'id': 1, 'username': 'Alice', 'text': 'old', 'created_at': '2026-07-21T10:00:00', 'likes_count': 0}]
        comments_round_2 = comments_round_1 + [{'id': 2, 'username': 'Bob', 'text': '<new>', 'created_at': '2026-07-21T10:01:00', 'likes_count': 3}]
        calls = []
        orig_fetch = poster.fetch_post_comments
        orig_tg = poster.tg_request
        poster.fetch_post_comments = lambda cfg, pid: comments_round_1
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True, 'result': {'message_id': 900}}
        try:
            sent, marked = poster.sync_post_comments(c, con, post(post_id=902), send_existing=False)
            poster.fetch_post_comments = lambda cfg, pid: comments_round_2
            sent2, marked2 = poster.sync_post_comments(c, con, post(post_id=902), send_existing=False)
        finally:
            poster.fetch_post_comments = orig_fetch
            poster.tg_request = orig_tg

        self.assertEqual((sent, marked), (0, 1))
        self.assertEqual((sent2, marked2), (1, 0))
        self.assertEqual(calls[0][0], 'sendMessage')
        self.assertEqual(calls[0][1]['chat_id'], '-100222')
        self.assertEqual(calls[0][1]['reply_to_message_id'], 777)
        self.assertIn('&lt;new&gt;', calls[0][1]['text'])
        rows = con.execute('SELECT comment_id, telegram_message_id FROM dragonfly_comments WHERE post_id=902 ORDER BY comment_id').fetchall()
        self.assertEqual(rows, [(1, None), (2, 900)])

    def test_sync_comments_send_existing_posts_seeded_rows(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        c.telegram_token = 'tg'
        poster.save_discussion_message(
            con,
            post_id=904,
            role='last',
            channel_chat_id='@channel',
            channel_message_id=555,
            discussion_chat_id='-100222',
            discussion_message_id=777,
        )
        comment = {'id': 10, 'username': 'Alice', 'text': 'seeded', 'created_at': '2026-07-21T10:00:00', 'likes_count': 0}
        poster.mark_comment_sent(con, post_id=904, comment=comment, telegram_chat_id=None, telegram_message_id=None)
        calls = []
        orig_fetch = poster.fetch_post_comments
        orig_tg = poster.tg_request
        poster.fetch_post_comments = lambda cfg, pid: [comment]
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True, 'result': {'message_id': 901}}
        try:
            sent, marked = poster.sync_post_comments(c, con, post(post_id=904), send_existing=True)
        finally:
            poster.fetch_post_comments = orig_fetch
            poster.tg_request = orig_tg

        self.assertEqual((sent, marked), (1, 0))
        self.assertEqual(calls[0][1]['reply_to_message_id'], 777)
        row = con.execute('SELECT telegram_message_id FROM dragonfly_comments WHERE post_id=904 AND comment_id=10').fetchone()
        self.assertEqual(row, (901,))

    def test_sync_post_stats_edits_text_when_counts_change(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        poster.mark_sent(con, post(post_id=889))
        poster.record_telegram_message(
            con,
            post(post_id=889, likes_count=2, comments_count=1),
            chat_id='@channel',
            message_id=500,
            message_kind='text',
            base_html='base <b>html</b>',
        )
        calls = []
        orig = poster.tg_request
        poster.tg_request = lambda cfg, method, payload: calls.append((method, payload)) or {'ok': True, 'result': {'message_id': 500}}
        try:
            changed = poster.sync_post_stats(c, con, post(post_id=889, likes_count=3, comments_count=4))
        finally:
            poster.tg_request = orig

        self.assertTrue(changed)
        self.assertEqual(calls[0][0], 'editMessageText')
        self.assertEqual(calls[0][1]['message_id'], 500)
        self.assertIn('❤️ 3', calls[0][1]['text'])
        self.assertIn('💬 4', calls[0][1]['text'])
        row = con.execute("SELECT last_likes, last_comments FROM telegram_messages WHERE post_id=889").fetchone()
        self.assertEqual(row, (3, 4))

    def test_long_split_post_records_last_role_for_final_part(self):
        con = poster.init_db(Path(':memory:'))
        c = cfg()
        c.dry_run = False
        seq = {'message_id': 100}
        orig = poster.tg_request
        orig_sleep = poster.time.sleep

        def fake_tg_request(cfg, method, payload):
            seq['message_id'] += 1
            return {'ok': True, 'result': {'message_id': seq['message_id']}}

        poster.tg_request = fake_tg_request
        poster.time.sleep = lambda *_: None
        try:
            n = poster.send_new_posts(c, con, [post(post_id=890, description='абвгд ' * 900)])
        finally:
            poster.tg_request = orig
            poster.time.sleep = orig_sleep

        self.assertEqual(n, 1)
        rows = dict(con.execute("SELECT role, message_id FROM telegram_messages WHERE post_id=890").fetchall())
        self.assertIn('main', rows)
        self.assertIn('last', rows)
        self.assertLess(rows['main'], rows['last'])

    def test_failed_posts_stop_after_attempt_limit(self):
        con = poster.init_db(Path(':memory:'))
        p = post(post_id=777, description='text')
        for _ in range(poster.DEFAULT_MAX_ATTEMPTS):
            poster.mark_failed(con, p, 'boom')

        self.assertTrue(poster.is_exhausted(con, 777, poster.DEFAULT_MAX_ATTEMPTS))

    def test_cleanup_keeps_recent_sent_and_preserves_failed(self):
        con = poster.init_db(Path(':memory:'))
        for pid in range(1, 8):
            poster.mark_sent(con, post(post_id=pid))
        poster.mark_failed(con, post(post_id=99), 'boom')

        deleted = poster.cleanup_sent(con, keep_sent=3)

        self.assertEqual(deleted, 4)
        remaining_sent = [r[0] for r in con.execute("select post_id from sent_posts where status='sent' order by post_id").fetchall()]
        self.assertEqual(remaining_sent, [5, 6, 7])
        failed = con.execute("select post_id from sent_posts where status='failed'").fetchall()
        self.assertEqual(failed, [(99,)])

    def test_more_than_ten_photos_are_sent_as_multiple_media_groups(self):
        photos = [{'url': f'/p{i}.jpg'} for i in range(23)]
        with CaptureTelegram() as calls:
            poster.send_post(cfg(), post(description='album', photos=photos))

        groups = [payload for method, payload in calls if method == 'sendMediaGroup']
        self.assertEqual([len(g['media']) for g in groups], [10, 10, 3])
        self.assertIn('caption', groups[0]['media'][0])
        for g in groups[1:]:
            for item in g['media']:
                self.assertNotIn('caption', item)


if __name__ == '__main__':
    unittest.main(verbosity=2)
