#!/usr/bin/env python3
"""Unit tests for the image-owned HTTPS onboarding frontend."""

from __future__ import annotations

import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path


FIRSTBOOT = Path(__file__).resolve().parents[1] / "firstboot"
REPOSITORY_ROOT = FIRSTBOOT.parents[1]
os.sys.path.insert(0, str(FIRSTBOOT))

import pi_router_onboarding_frontend as frontend  # noqa: E402
import pi_router_image_state as image_state  # noqa: E402


def credential_frame(username: str, password: str) -> bytearray:
    username_bytes = username.encode("utf-8")
    password_bytes = password.encode("utf-8")
    return bytearray(
        bytes(
            (
                frontend.CREDENTIAL_VERSION,
                len(username_bytes) >> 8,
                len(username_bytes) & 0xFF,
                len(password_bytes) >> 8,
                len(password_bytes) & 0xFF,
            )
        )
        + username_bytes
        + password_bytes
    )


class CredentialFrameTests(unittest.TestCase):
    def test_accepts_exact_bounded_utf8_envelope(self) -> None:
        frame = credential_frame("router@example.test", "correct horse")
        self.assertEqual(
            frontend.validate_credential_frame(frame),
            (len("router@example.test"), len("correct horse")),
        )

    def test_rejects_wrong_length_version_and_forbidden_bytes(self) -> None:
        cases = [
            bytearray(),
            bytearray(b"\x02\x00\x01\x00\x01ab"),
            bytearray(b"\x01\x00\x02\x00\x01ab"),
            credential_frame("bad\nname", "secret"),
            credential_frame("name", "bad\x00secret"),
            credential_frame("name", "bad\x03secret"),
            bytearray(b"\x01\x00\x01\x00\x01\xffa"),
        ]
        for frame in cases:
            with self.subTest(frame_length=len(frame)):
                with self.assertRaises(frontend.InvalidCredentialFrame):
                    frontend.validate_credential_frame(frame)

    def test_zero_bytearray_overwrites_complete_frame(self) -> None:
        frame = credential_frame("name", "secret")
        frontend.zero_bytearray(frame)
        self.assertEqual(frame, bytearray(len(frame)))


class ConfigAndAssetTests(unittest.TestCase):
    def test_loads_one_literal_lan_address(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "router.conf"
            path.write_text(
                '# fixed image input\nLAN_ADDR="192.168.50.1"\nLAN_PREFIX="24"\n',
                encoding="ascii",
            )
            self.assertEqual(
                frontend.load_lan_address(path, expected_owner=None),
                "192.168.50.1",
            )

    def test_rejects_duplicate_or_shell_expanded_lan_address(self) -> None:
        values = [
            'LAN_ADDR="192.168.50.1"\nLAN_ADDR="192.168.50.2"\n',
            'LAN_ADDR="$(hostname)"\n',
            'LAN_ADDR="127.0.0.1"\n',
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "router.conf"
            for value in values:
                with self.subTest(value=value):
                    path.write_text(value, encoding="ascii")
                    with self.assertRaises(frontend.FrontendError):
                        frontend.load_lan_address(path, expected_owner=None)

    def test_loads_the_production_web_version_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "version.txt"
            path.write_text("v1.2.3-test.4\n", encoding="ascii")
            self.assertEqual(
                frontend.load_router_version(path, expected_owner=None),
                "v1.2.3-test.4",
            )

            path.write_text("not-a-version\n", encoding="ascii")
            with self.assertRaises(frontend.FrontendError):
                frontend.load_router_version(path, expected_owner=None)

    def test_asset_bundle_requires_and_replaces_csrf_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_bytes(
                b'<meta content="__PI_ROUTER_CSRF_TOKEN__">'
            )
            (root / "app.js").write_bytes(b"void 0;")
            (root / "styles.css").write_bytes(b"body{}")
            (root / "favicon.svg").write_bytes(b"<svg/>")
            assets = frontend.AssetBundle.load(root)
            token = "A" * 43
            rendered = assets.index_for(token)
            self.assertIn(token.encode("ascii"), rendered)
            self.assertNotIn(b"__PI_ROUTER_CSRF_TOKEN__", rendered)


class StatusPresentationContractTests(unittest.TestCase):
    def test_frontend_and_durable_status_enums_are_synchronized(self) -> None:
        self.assertEqual(
            set(frontend._STATUS_SUMMARIES),
            set(image_state.STATUS_CODES),
        )

    def test_status_summaries_are_nonempty_strings(self) -> None:
        for code, summary in frontend._STATUS_SUMMARIES.items():
            with self.subTest(code=code):
                self.assertIsInstance(summary, str)
                self.assertTrue(summary)


class FakeSocket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.timeout: float | None = None
        self.connected: str | None = None
        self.sent: bytes | None = None
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, path: str) -> None:
        self.connected = path

    def send(self, value: object) -> int:
        self.sent = bytes(value)  # type: ignore[arg-type]
        return len(self.sent)

    def recvmsg(self, maximum: int) -> tuple[bytes, list[object], int, None]:
        return self.response, [], 0, None

    def close(self) -> None:
        self.closed = True


class BrokerClientTests(unittest.TestCase):
    def test_status_uses_one_fixed_seqpacket_request(self) -> None:
        fake = FakeSocket(b"waiting-onboarding\n")
        calls: list[tuple[int, int]] = []

        def factory(family: int, kind: int) -> FakeSocket:
            calls.append((family, kind))
            return fake

        client = frontend.BrokerClient(
            Path("/run/pi-router-image/onboarding.sock"), socket_factory=factory
        )
        self.assertEqual(client.status(), "waiting-onboarding")
        self.assertEqual(fake.sent, frontend.STATUS_REQUEST)
        self.assertEqual(calls, [(frontend.socket.AF_UNIX, frontend.socket.SOCK_SEQPACKET)])
        self.assertTrue(fake.closed)

    def test_submit_sends_exact_binary_envelope_and_accepts_fixed_response(self) -> None:
        fake = FakeSocket(b"accepted\n")
        client = frontend.BrokerClient(
            Path("/run/pi-router-image/onboarding.sock"),
            socket_factory=lambda _family, _kind: fake,
        )
        frame = credential_frame("name", "secret")
        expected = bytes(frame)
        self.assertEqual(client.submit(frame), "accepted")
        self.assertEqual(fake.sent, expected)

    def test_unknown_backend_response_fails_closed(self) -> None:
        fake = FakeSocket(b"accepted with arbitrary detail\n")
        client = frontend.BrokerClient(
            Path("/run/pi-router-image/onboarding.sock"),
            socket_factory=lambda _family, _kind: fake,
        )
        with self.assertRaises(frontend.BackendUnavailable):
            client.submit(credential_frame("name", "secret"))


class SessionAndRateLimitTests(unittest.TestCase):
    def test_first_live_session_claims_onboarding(self) -> None:
        now = [10.0]
        token_values = iter(("A" * 43, "B" * 43, "C" * 43, "D" * 43))
        sessions = frontend.SessionStore(
            clock=lambda: now[0], token_factory=lambda _count: next(token_values)
        )
        first = sessions.create()
        second = sessions.create()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertTrue(sessions.claim(first[0]))
        self.assertTrue(sessions.claim(first[0]))
        self.assertFalse(sessions.claim(second[0]))

        now[0] += frontend.SESSION_SECONDS + 1
        self.assertFalse(sessions.claim(first[0]))

    def test_authenticated_access_slides_session_expiry(self) -> None:
        now = [0.0]
        tokens = iter(("A" * 43, "B" * 43))
        sessions = frontend.SessionStore(
            clock=lambda: now[0],
            token_factory=lambda _count: next(tokens),
            lifetime=100,
        )
        created = sessions.create()
        assert created is not None
        session_id, csrf = created
        now[0] = 90
        self.assertEqual(sessions.csrf_for(session_id), csrf)
        now[0] = 150
        self.assertEqual(sessions.csrf_for(session_id), csrf)
        now[0] = 251
        self.assertIsNone(sessions.csrf_for(session_id))

    def test_full_store_evicts_oldest_unclaimed_not_claimant(self) -> None:
        tokens = iter(
            ("A" * 43, "B" * 43, "C" * 43, "D" * 43, "E" * 43, "F" * 43)
        )
        sessions = frontend.SessionStore(
            token_factory=lambda _count: next(tokens), maximum=2
        )
        first = sessions.create()
        claimant = sessions.create()
        assert first is not None and claimant is not None
        self.assertTrue(sessions.claim(claimant[0]))
        replacement = sessions.create()
        assert replacement is not None
        self.assertIsNone(sessions.csrf_for(first[0]))
        self.assertIsNotNone(sessions.csrf_for(claimant[0]))
        self.assertIsNotNone(sessions.csrf_for(replacement[0]))

    def test_rate_limiter_applies_cooldown_and_sliding_limits(self) -> None:
        now = [0.0]
        limiter = frontend.RateLimiter(
            clock=lambda: now[0],
            window=100,
            client_limit=2,
            global_limit=3,
            cooldown=3,
        )
        self.assertEqual(limiter.allow("192.168.50.10"), (True, 0))
        allowed, retry = limiter.allow("192.168.50.10")
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry, 1)
        now[0] = 4
        self.assertEqual(limiter.allow("192.168.50.10"), (True, 0))
        now[0] = 8
        self.assertFalse(limiter.allow("192.168.50.10")[0])
        self.assertEqual(limiter.allow("192.168.50.11"), (True, 0))
        self.assertFalse(limiter.allow("192.168.50.12")[0])


class FakeBackend:
    def __init__(self) -> None:
        self.status_code = "waiting-onboarding"
        self.result = "accepted"
        self.frames: list[bytearray] = []
        self.status_calls = 0

    def status(self) -> str:
        self.status_calls += 1
        return self.status_code

    def submit(self, frame: bytearray) -> str:
        self.frames.append(frame)
        return self.result


class HTTPFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        token_counter = iter(range(1, 100))

        def token_factory(_count: int) -> str:
            value = next(token_counter)
            return ("A" * 41) + f"{value:02d}"

        assets = frontend.AssetBundle(
            index=b'<meta name="pi-router-csrf" content="__PI_ROUTER_CSRF_TOKEN__">',
            javascript=b"void 0;",
            stylesheet=b"body{}",
            favicon=b"<svg/>",
        )
        self.backend = FakeBackend()
        settings = frontend.FrontendSettings(
            "127.0.0.1",
            port=8080,
            provisioning_network="127.0.0.0/8",
            router_version="v1.2.3-test.4",
        )
        application = frontend.FrontendApplication(
            settings,
            assets,
            self.backend,
            sessions=frontend.SessionStore(token_factory=token_factory),
            rate_limiter=frontend.RateLimiter(
                cooldown=0, client_limit=20, global_limit=20
            ),
        )
        self.server = frontend.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), application
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | bytearray | None = None,
    ) -> tuple[http.client.HTTPResponse, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.putrequest(method, target, skip_host=True)
        values = {"Host": "127.0.0.1:8080", **(headers or {})}
        if body is not None and "Content-Length" not in values:
            values["Content-Length"] = str(len(body))
        for name, value in values.items():
            connection.putheader(name, value)
        connection.endheaders(body)
        response = connection.getresponse()
        response_body = response.read()
        connection.close()
        return response, response_body

    def new_session(self) -> tuple[str, str]:
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        cookie = response.getheader("Set-Cookie")
        assert cookie is not None
        match = re.search(rb'content="([A-Za-z0-9_-]{43})"', body)
        assert match is not None
        return cookie.split(";", 1)[0], match.group(1).decode("ascii")

    def test_index_has_secure_cookie_and_security_headers(self) -> None:
        response, _body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        cookie = response.getheader("Set-Cookie")
        assert cookie is not None
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("no-store", response.getheader("Cache-Control", ""))
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy", ""))
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_shared_stylesheet_and_onboarding_script_are_served(self) -> None:
        stylesheet, stylesheet_body = self.request("GET", "/src/web/styles.css")
        self.assertEqual(stylesheet.status, 200)
        self.assertEqual(stylesheet_body, b"body{}")
        self.assertEqual(
            stylesheet.getheader("Content-Type"),
            "text/css; charset=utf-8",
        )

        javascript, javascript_body = self.request("GET", "/onboarding-app.js")
        self.assertEqual(javascript.status, 200)
        self.assertEqual(javascript_body, b"void 0;")
        self.assertEqual(
            javascript.getheader("Content-Type"),
            "application/javascript; charset=utf-8",
        )

        favicon, favicon_body = self.request("GET", "/src/web/favicon.svg")
        self.assertEqual(favicon.status, 200)
        self.assertEqual(favicon_body, b"<svg/>")
        self.assertEqual(favicon.getheader("Content-Type"), "image/svg+xml")

    def test_reload_reuses_live_session_so_claimant_can_retry(self) -> None:
        cookie, csrf = self.new_session()
        response, body = self.request("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Set-Cookie", "").startswith(cookie + ";"))
        self.assertIn(csrf.encode("ascii"), body)

    def test_status_requires_session_and_returns_fixed_presentation(self) -> None:
        unauthenticated, _body = self.request("GET", "/api/status")
        self.assertEqual(unauthenticated.status, 401)
        cookie, _csrf = self.new_session()
        response, body = self.request(
            "GET", "/api/status", headers={"Cookie": cookie}
        )
        self.assertEqual(response.status, 200)
        document = json.loads(body)
        self.assertEqual(document["status"], "waiting-onboarding")
        self.assertEqual(document["summary"], "Ready for Proton sign-in")
        self.assertEqual(document["version"], "v1.2.3-test.4")
        self.assertEqual(set(document), {"status", "summary", "version"})
        self.assertTrue(response.getheader("Set-Cookie", "").startswith(cookie + ";"))
        self.assertIn(
            f"Max-Age={frontend.SESSION_SECONDS}",
            response.getheader("Set-Cookie", ""),
        )

        second, _body = self.request(
            "GET", "/api/status", headers={"Cookie": cookie}
        )
        self.assertEqual(second.status, 200)
        self.assertEqual(self.backend.status_calls, 1)

    def test_rejects_query_absolute_origin_and_wrong_host(self) -> None:
        query, _body = self.request("GET", "/?anything=1")
        self.assertEqual(query.status, 400)
        absolute, _body = self.request("GET", "https://127.0.0.1:8080/")
        self.assertEqual(absolute.status, 400)
        wrong_host, _body = self.request(
            "GET", "/", headers={"Host": "router.local:8080"}
        )
        self.assertEqual(wrong_host.status, 400)

    def test_post_requires_exact_origin_cookie_csrf_and_content_type(self) -> None:
        cookie, csrf = self.new_session()
        frame = credential_frame("name", "secret")
        response, _body = self.request(
            "POST",
            "/api/onboard",
            headers={
                "Cookie": cookie,
                "Origin": "https://attacker.invalid",
                "X-Pi-Router-CSRF": csrf,
                "Content-Type": frontend.CREDENTIAL_CONTENT_TYPE,
            },
            body=frame,
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.backend.frames, [])

    def test_valid_post_is_accepted_and_server_buffer_is_zeroed(self) -> None:
        cookie, csrf = self.new_session()
        frame = credential_frame("name", "secret")
        response, body = self.request(
            "POST",
            "/api/onboard",
            headers={
                "Cookie": cookie,
                "Origin": "https://127.0.0.1:8080",
                "X-Pi-Router-CSRF": csrf,
                "Content-Type": frontend.CREDENTIAL_CONTENT_TYPE,
            },
            body=frame,
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(json.loads(body), {"status": "accepted"})
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.assertEqual(len(self.backend.frames), 1)
        self.assertEqual(self.backend.frames[0], bytearray(len(frame)))

    def test_first_valid_submitting_session_claims_onboarding(self) -> None:
        first_cookie, first_csrf = self.new_session()
        second_cookie, second_csrf = self.new_session()

        def submit(cookie: str, csrf: str) -> int:
            response, _body = self.request(
                "POST",
                "/api/onboard",
                headers={
                    "Cookie": cookie,
                    "Origin": "https://127.0.0.1:8080",
                    "X-Pi-Router-CSRF": csrf,
                    "Content-Type": frontend.CREDENTIAL_CONTENT_TYPE,
                },
                body=credential_frame("name", "secret"),
            )
            return response.status

        self.assertEqual(submit(first_cookie, first_csrf), 202)
        self.assertEqual(submit(second_cookie, second_csrf), 423)
        self.assertEqual(len(self.backend.frames), 1)


class ServiceDefinitionTests(unittest.TestCase):
    def test_service_is_static_hardened_and_uses_systemd_credentials(self) -> None:
        service = (FIRSTBOOT / "pi-router-onboarding.service").read_text(
            encoding="utf-8"
        )
        self.assertEqual(frontend.DEFAULT_PORT, 8080)
        self.assertNotIn("[Install]", service)
        self.assertIn("User=pi-router-onboarding", service)
        self.assertIn("Requires=pi-router-onboarding-backend.service", service)
        self.assertIn("BindsTo=pi-router-onboarding-backend.service", service)
        self.assertIn("After=network.target pi-router-onboarding-backend.service", service)
        self.assertIn("LoadCredential=tls.crt:", service)
        self.assertIn("LoadCredential=tls.key:", service)
        self.assertIn("RestrictAddressFamilies=AF_INET AF_UNIX", service)
        self.assertIn("IPAddressDeny=any", service)
        self.assertIn("--port 8080", service)
        self.assertIn(
            "--version-file /usr/local/share/pi-router-web/version.txt", service
        )
        self.assertIn(
            "ConditionPathExists=/usr/local/share/pi-router-web/version.txt", service
        )
        self.assertIn("SocketBindAllow=ipv4:tcp:8080", service)
        self.assertNotIn("8443", service)
        self.assertIn("LimitCORE=0", service)
        self.assertIn("MemorySwapMax=0", service)
        self.assertNotIn("0.0.0.0", service)

    def test_browser_assets_disable_password_saving_and_recover_sessions(self) -> None:
        index = (FIRSTBOOT / "onboarding-index.html").read_text(encoding="utf-8")
        javascript = (FIRSTBOOT / "onboarding-app.js").read_text(encoding="utf-8")
        self.assertGreaterEqual(index.count('autocomplete="off"'), 2)
        self.assertGreaterEqual(index.count('autocapitalize="none"'), 2)
        self.assertGreaterEqual(index.count('spellcheck="false"'), 2)
        self.assertEqual(index.count('type="password"'), 2)
        self.assertNotIn('type="text"', index)
        self.assertEqual(index.count('class="concealed-credential"'), 2)
        self.assertEqual(index.count('class="concealed-credential-mask"'), 2)
        self.assertIn("Do not let the browser save this password", index)
        self.assertIn("same fixed mask", index)
        self.assertIn('href="/src/web/styles.css"', index)
        self.assertIn('src="/src/web/favicon.svg"', index)
        self.assertIn('class="brand-icon"', index)
        self.assertIn('id="routerVersion"', index)
        self.assertIn("Onboarding | v0.0.1", index)
        self.assertNotIn("First boot setup", index)
        self.assertIn('class="message success"', index)
        self.assertGreaterEqual(index.count('role="status"'), 2)
        self.assertIn('id="form-result"', index)
        self.assertNotIn("onboarding-form-result", index)
        self.assertNotIn("onboarding-styles.css", index)
        self.assertIn('src="./onboarding-app.js"', index)
        self.assertNotIn("Router checks", index)
        for removed_id in (
            "status-badge",
            "status-hardware",
            "status-cables",
            "status-wan",
            "status-setup",
            "status-error",
        ):
            with self.subTest(removed_id=removed_id):
                self.assertNotIn(removed_id, index)
                self.assertNotIn(removed_id, javascript)
        self.assertIn("response.status === 401", javascript)
        self.assertIn("runningPostObserved", javascript)
        self.assertIn("Sign-in did not complete", javascript)
        self.assertNotIn('id="production-handoff"', index)
        self.assertNotIn("http://192.168.50.1:8080/", index)
        self.assertIn('if (documentValue.status === "rebooting-after-post")', javascript)
        self.assertIn("router will reboot automatically", javascript)
        self.assertIn(
            'const PRODUCTION_URL = "http://192.168.50.1:8080/"', javascript
        )
        self.assertIn("PRODUCTION_REDIRECT_DELAY_MS = 300000", javascript)
        self.assertIn("window.location.replace(PRODUCTION_URL)", javascript)
        self.assertIn("function scheduleProductionRedirect()", javascript)
        self.assertIn(
            '`Onboarding | ${documentValue.version || "v0.0.1"}`',
            javascript,
        )
        self.assertIn('element.className = `message ${kind}`', javascript)
        self.assertIn('element.className = "message"', javascript)
        self.assertIn("element.hidden = false", javascript)
        self.assertIn("element.hidden = true", javascript)
        self.assertNotIn("showProductionHandoff", javascript)
        self.assertNotIn("productionHandoff", javascript)
        self.assertIn("function updateCredentialMask(field)", javascript)
        self.assertIn('field.value.length > 0', javascript)

        stylesheet = (REPOSITORY_ROOT / "src/web/styles.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(".concealed-credential.has-value", stylesheet)
        self.assertRegex(
            stylesheet,
            r"(?s)\.concealed-credential\s*\{.*?width:\s*50%;",
        )
        self.assertIn("caret-color: transparent", stylesheet)
        self.assertIn("opacity: 0", stylesheet)
        self.assertIn(".message.success", stylesheet)
        self.assertIn(".message.error", stylesheet)
        self.assertNotIn(".onboarding-form-result", stylesheet)


class TLSDispatchTests(unittest.TestCase):
    class AcceptedSocket:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)

    class SecuredSocket(AcceptedSocket):
        def __init__(self) -> None:
            super().__init__()
            self.handshakes = 0

        def do_handshake(self) -> None:
            self.handshakes += 1

    class TLSContext:
        def __init__(self, secured: "TLSDispatchTests.SecuredSocket") -> None:
            self.secured = secured
            self.calls: list[tuple[object, bool, bool]] = []

        def wrap_socket(
            self,
            request: object,
            *,
            server_side: bool,
            do_handshake_on_connect: bool,
        ) -> "TLSDispatchTests.SecuredSocket":
            self.calls.append(
                (request, server_side, do_handshake_on_connect)
            )
            return self.secured

    def test_tls_is_wrapped_and_handshaken_only_in_bounded_worker(self) -> None:
        assets = frontend.AssetBundle(
            index=b"__PI_ROUTER_CSRF_TOKEN__",
            javascript=b"",
            stylesheet=b"",
            favicon=b"",
        )
        application = frontend.FrontendApplication(
            frontend.FrontendSettings(
                "127.0.0.1", port=8080, provisioning_network="127.0.0.0/8"
            ),
            assets,
            FakeBackend(),
        )
        secured = self.SecuredSocket()
        context = self.TLSContext(secured)
        server = frontend.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0),
            application,
            tls_context=context,  # type: ignore[arg-type]
        )
        self.addCleanup(server.server_close)
        self.assertEqual(context.calls, [])

        accepted = self.AcceptedSocket()
        result = server._tls_request(accepted)  # type: ignore[arg-type]
        self.assertIs(result, secured)
        self.assertEqual(
            context.calls,
            [(accepted, True, False)],
        )
        self.assertEqual(accepted.timeouts, [frontend.HTTP_TIMEOUT_SECONDS])
        self.assertEqual(secured.timeouts, [frontend.HTTP_TIMEOUT_SECONDS])
        self.assertEqual(secured.handshakes, 1)


if __name__ == "__main__":
    unittest.main()
