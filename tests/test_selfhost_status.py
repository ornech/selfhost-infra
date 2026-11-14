"""Tests du relevé d'état (roles/status/files/selfhost-status.py).

    python3 -m unittest discover -s tests -v

Les analyseurs sont testés sur des sorties réelles (systemctl, HAProxy, mdstat,
nft, /proc/net/tcp). Les chemins qui parlent au réseau (sonde HTTP, socket
d'administration de HAProxy, certificat servi) le sont contre de faux serveurs
locaux : aucune dépendance à la machine qui exécute les tests.
"""
import contextlib
import datetime
import http.server
import importlib.util
import io
import json
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "roles/status/files/selfhost-status.py"
spec = importlib.util.spec_from_file_location("selfhost_status", SCRIPT)
st = importlib.util.module_from_spec(spec)
sys.modules["selfhost_status"] = st
spec.loader.exec_module(st)


class Parsing(unittest.TestCase):
    def test_parse_iso_restic_nanoseconds(self):
        when = st.parse_iso("2026-11-14T03:15:44.123456789+01:00")
        self.assertEqual(when.utcoffset(), datetime.timedelta(hours=1))
        self.assertEqual(when.microsecond, 123456)

    def test_parse_iso_zulu_and_short_fraction(self):
        self.assertEqual(st.parse_iso("2026-11-14T02:15:44Z").hour, 2)
        self.assertEqual(st.parse_iso("2026-11-14T02:15:44.5Z").microsecond, 500000)

    def test_parse_iso_rejects_garbage(self):
        with self.assertRaises(ValueError):
            st.parse_iso("hier")

    def test_parse_show_blocks(self):
        text = ("Id=a.service\nLoadState=loaded\nActiveState=active\n\n"
                "Id=b.service\nLoadState=not-found\nActiveState=inactive\n")
        blocks = st.parse_show(text)
        self.assertEqual([b["Id"] for b in blocks], ["a.service", "b.service"])
        self.assertEqual(blocks[1]["LoadState"], "not-found")

    def test_unit_rows(self):
        self.assertEqual(st.unit_row("a", {"LoadState": "loaded", "ActiveState": "active", "SubState": "running"})["level"], st.OK)
        self.assertEqual(st.unit_row("a", {"LoadState": "not-found"})["level"], st.CRIT)
        self.assertEqual(st.unit_row("a", {"LoadState": "loaded", "ActiveState": "failed", "Result": "exit-code"})["level"], st.CRIT)
        self.assertEqual(st.unit_row("a", {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead"})["level"], st.CRIT)
        self.assertEqual(st.unit_row("a", {"LoadState": "loaded", "ActiveState": "activating", "SubState": "start"})["level"], st.WARN)
        loop = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "NRestarts": "9"}
        self.assertEqual(st.unit_row("a", loop)["level"], st.WARN)

    def test_show_units_refuses_a_partial_answer(self):
        # moins de blocs que d'unités demandées : on ne devine pas, on renvoie None
        self.assertIsNone(st.show_units(["a", "b"], lambda args: (0, "Id=a\n", "")))
        self.assertIsNone(st.show_units(["a"], lambda args: (1, "", "")))

    def test_parse_mdstat_healthy_degraded_recovering(self):
        text = (
            "Personalities : [raid1]\n"
            "md0 : active raid1 sdb1[1] sda1[0]\n"
            "      976630464 blocks super 1.2 [2/2] [UU]\n"
            "      bitmap: 0/8 pages [0KB], 65536KB chunk\n\n"
            "md1 : active raid1 sdb2[1](F) sda2[0]\n"
            "      976630464 blocks super 1.2 [2/1] [U_]\n\n"
            "md2 : active raid1 sdd1[2] sdc1[0]\n"
            "      976630464 blocks super 1.2 [2/1] [U_]\n"
            "      [>....................]  recovery =  2.4% (23424/976630464) finish=95.4min\n"
            "unused devices: <none>\n")
        arrays = {a["name"]: a for a in st.parse_mdstat(text)}
        self.assertEqual(arrays["md0"]["pattern"], "UU")
        self.assertTrue(arrays["md1"]["failed"])
        self.assertEqual(arrays["md2"]["progress"], ("recovery", "2.4"))

    def test_raid_rows_levels(self):
        def level(text):
            with mock.patch.object(st, "read_text", return_value=text):
                return st.raid_rows()[0]["level"]
        ok = "md0 : active raid1 sdb1[1] sda1[0]\n      1 blocks super 1.2 [2/2] [UU]\n"
        bad = "md0 : active raid1 sda1[0]\n      1 blocks super 1.2 [2/1] [U_]\n"
        self.assertEqual(level(ok), st.OK)
        self.assertEqual(level(bad), st.CRIT)
        self.assertEqual(level("Personalities :\nunused devices: <none>\n"), st.INFO)

    def test_parse_proc_net_tcp_v4_and_v6(self):
        v4 = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
              "   0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 111 1 0\n"
              "   1: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 222 1 0\n"
              "   2: 0100007F:1F90 0100007F:9999 01 00000000:00000000 00:00000000 00000000     0        0 333 1 0\n")
        self.assertEqual(st.parse_proc_net_tcp(v4), [("0.0.0.0", 22, "111"), ("127.0.0.1", 8080, "222")])
        v6 = ("  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
              "   0: 00000000000000000000000000000000:01BB 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 444 1 0\n"
              "   1: 00000000000000000000000001000000:0CEA 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 555 1 0\n")
        self.assertEqual(st.parse_proc_net_tcp(v6, ipv6=True), [("::", 443, "444"), ("::1", 3306, "555")])
        self.assertTrue(st.is_loopback("::1") and st.is_loopback("127.0.0.1"))
        self.assertFalse(st.is_loopback("::") or st.is_loopback("192.168.1.5"))

    def test_listening_row_flags_unexpected_port_once(self):
        cfg = {"listen_allowed": [22, 80, 443, 8443]}
        v4 = ("hdr\n   0: 00000000:0016 00000000:0000 0A 0:0 0:0 0 0 0 1 1 0\n"
              "   1: 00000000:0CEA 00000000:0000 0A 0:0 0:0 0 0 0 2 1 0\n")
        v6 = ("hdr\n   0: 00000000000000000000000000000000:0CEA 00000000000000000000000000000000:0000 0A 0:0 0:0 0 0 0 3 1 0\n")
        files = {"/proc/net/tcp": v4, "/proc/net/tcp6": v6}
        with mock.patch.object(st, "read_text", side_effect=lambda p: files[p]), \
                mock.patch.object(st, "process_of_socket", return_value="mysqld"):
            row = st.listening_row(cfg)
        self.assertEqual(row["level"], st.WARN)
        self.assertEqual(row["detail"].count("3306"), 1)
        self.assertIn("mysqld", row["detail"])

    def test_parse_nft_policy(self):
        text = json.dumps({"nftables": [
            {"metainfo": {"version": "1.1.3"}},
            {"chain": {"family": "inet", "table": "host_fw", "name": "input", "type": "filter",
                       "hook": "input", "prio": 0, "policy": "drop"}},
            {"rule": {"family": "inet", "table": "host_fw", "chain": "input"}}]})
        self.assertEqual(st.parse_nft_policy(text, "host_fw", "input"), "drop")
        self.assertIsNone(st.parse_nft_policy(text, "host_fw", "forward"))

    def test_firewall_rows(self):
        def rows(chain_json, tables, code=0):
            answers = iter([(code, chain_json, ""), (0, tables, "")])
            with mock.patch.object(st, "run", side_effect=lambda *a, **k: next(answers)):
                return [r["level"] for r in st.firewall_rows()]
        chain = lambda policy: json.dumps({"nftables": [{"chain": {  # noqa: E731
            "family": "inet", "table": "host_fw", "name": "input", "policy": policy}}]})
        tables = "table inet host_fw\ntable ip crowdsec\ntable ip6 crowdsec6\n"
        self.assertEqual(rows(chain("drop"), tables), [st.OK, st.OK])
        self.assertEqual(rows(chain("accept"), tables), [st.CRIT, st.OK])      # pare-feu ouvert
        self.assertEqual(rows("", tables, code=1), [st.CRIT, st.OK])           # table absente
        self.assertEqual(rows(chain("drop"), "table inet host_fw\n"), [st.OK, st.CRIT])   # bouncer muet

    def test_parse_sshd_config(self):
        config = st.parse_sshd_config("passwordauthentication no\npermitrootlogin no\nallowusers jf freebox\n")
        self.assertEqual(config["passwordauthentication"], "no")
        self.assertEqual(config["allowusers"], "jf freebox")

    def test_parse_openssl_cert_and_days(self):
        info = st.parse_openssl_cert("notAfter=Feb  1 12:00:00 2027 GMT\nissuer=C = US, O = (STAGING) Let's Encrypt, CN = (STAGING) Ersatz Edamame E1\n")
        self.assertEqual(info["not_after"], datetime.datetime(2027, 2, 1, 12, tzinfo=datetime.timezone.utc))
        self.assertIn("STAGING", info["issuer"])
        self.assertEqual([st.cert_days_level(d) for d in (60, 20, 6)], [st.OK, st.WARN, st.CRIT])

    def test_parse_apt_simulation(self):
        text = ("Inst libc6 [2.41-12] (2.41-12+deb13u1 Debian-Security:13/stable-security [arm64])\n"
                "Inst vim [2:9.1] (2:9.2 Debian:13.1/stable [arm64])\n"
                "Conf libc6 (2.41-12+deb13u1 Debian-Security:13/stable-security [arm64])\n")
        self.assertEqual(st.parse_apt_simulation(text), (2, 1))

    def test_newer_kernel_same_flavour_only(self):
        installed = ["6.12.48+deb13-arm64", "6.12.57+deb13-arm64", "6.13.1+deb13-amd64"]
        self.assertEqual(st.newer_kernel("6.12.48+deb13-arm64", installed), "6.12.57+deb13-arm64")
        self.assertIsNone(st.newer_kernel("6.12.57+deb13-arm64", installed))

    def test_parse_haproxy_stat(self):
        text = ("# pxname,svname,qcur,status,lastchg,check_status,req_tot,hrsp_5xx,dreq,\n"
                "fe_public,FRONTEND,,OPEN,,,120,3,5,\n"
                "be_pub_www,www,0,UP,3600,L4OK,,,,\n"
                "be_pub_www,BACKEND,0,UP,3600,,,,,\n\n")
        rows = st.parse_haproxy_stat(text)
        server = [r for r in rows if r["svname"] == "www"][0]
        self.assertEqual((server["pxname"], server["status"], server["lastchg"]), ("be_pub_www", "UP", "3600"))
        self.assertEqual(st.parse_haproxy_stat("pas du CSV"), [])


class Rows(unittest.TestCase):
    site = {"host": "www.x.fr", "kind": "pub", "backend": "be_pub_www", "server": "www", "port": 8081}

    def row(self, status, probe, extra=None, error=None):
        servers = {("be_pub_www", "www"): {"status": status, "lastchg": "90", "check_status": "L4TOUT"}} if status else {}
        return st.site_row({**self.site, **(extra or {})}, servers, error, probe)

    def test_site_levels(self):
        self.assertEqual(self.row("UP", (200, 12))["level"], st.OK)
        self.assertEqual(self.row("DOWN", (200, 12))["level"], st.CRIT)
        self.assertEqual(self.row("UP", (503, 12))["level"], st.CRIT)
        self.assertEqual(self.row("UP", (404, 12))["level"], st.WARN)
        self.assertEqual(self.row("UP", (401, 12), {"probe_ok": [401]})["level"], st.OK)
        self.assertEqual(self.row("UP", ConnectionRefusedError())["level"], st.CRIT)
        self.assertEqual(self.row(None, (200, 12))["level"], st.WARN)
        self.assertEqual(self.row(None, (200, 12), error="socket injoignable")["level"], st.UNKNOWN)

    def test_snapshot_row_ages(self):
        now = 1_000_000_000.0
        def level(age_h):
            return st.snapshot_row("d", {"ok": True, "last": now - age_h * 3600, "count": 7}, 36, now)["level"]
        self.assertEqual((level(2), level(40), level(80)), (st.OK, st.WARN, st.CRIT))
        self.assertEqual(st.snapshot_row("d", {"ok": True, "last": None, "count": 0}, 36, now)["level"], st.CRIT)
        self.assertEqual(st.snapshot_row("d", {"ok": False, "code": 1}, 36, now)["level"], st.CRIT)
        self.assertEqual(st.snapshot_row("d", {"ok": False, "code": "commande absente"}, 36, now)["level"], st.UNKNOWN)

    def test_restic_error_output_is_never_kept(self):
        # restic cite l'URL du dépôt (identifiants compris) dans ses erreurs
        leak = "unable to open repository at rest:http://u:MOTDEPASSE@10.8.0.2:8000/"
        with mock.patch.object(st, "run", return_value=(1, "", leak)):
            result = st.fetch_snapshots("distant")
        self.assertNotIn("MOTDEPASSE", json.dumps(result))
        row = st.snapshot_row("distant", result, 36, 0)
        self.assertNotIn("MOTDEPASSE", json.dumps(row))

    def test_cached_keeps_only_accepted_results(self):
        state, calls = {}, []
        def fetch():
            calls.append(1)
            return {"ok": len(calls) > 1}
        for _ in range(3):
            st.cached(state, "k", 3600, fetch, keep=lambda v: v["ok"])
        self.assertEqual(len(calls), 2)       # l'échec est retenté, le succès est mémorisé


class Rendering(unittest.TestCase):
    def report(self, rows):
        return {"domain": "x.fr", "generated_at": 1_800_000_000, "generated_iso": "2027-01-15T10:00:00+01:00",
                "stale_after": 900, "overall": st.CRIT, "counts": {l: 0 for l in st.LABELS},
                "groups": [{"id": "g", "title": "Groupe <b>", "checks": rows}]}

    def test_html_escapes_everything(self):
        rows = [st.chk("<script>alert(1)</script>", st.CRIT, "x\" onload=\"y", url='https://a/"><script>')]
        page = st.render_html(self.report(rows))
        self.assertNotIn("<script>alert", page)
        self.assertNotIn('"><script>', page)
        self.assertIn("&lt;script&gt;", page)

    def test_html_declares_staleness_and_csp(self):
        page = st.render_html(self.report([st.chk("a", st.OK, "b")]))
        self.assertIn('data-generated="1800000000"', page)
        self.assertIn('data-stale-after="900"', page)
        self.assertIn("default-src 'none'", page)
        self.assertNotIn("<style", page)        # pas de style en ligne : CSP style-src 'self'
        self.assertNotIn(" onclick", page)

    def test_worst_level_wins(self):
        self.assertEqual(st.worst(st.OK, st.WARN, st.UNKNOWN), st.WARN)
        self.assertEqual(st.worst(st.UNKNOWN, st.CRIT, st.WARN), st.CRIT)
        self.assertEqual(st.worst(st.OK, st.UNKNOWN), st.UNKNOWN)

    def test_group_failure_becomes_unknown_not_a_crash(self):
        def boom(cfg, state):
            raise RuntimeError("panne")
        group = st.run_group(("g", "Titre", boom), {}, {})
        self.assertEqual(group["checks"][0]["level"], st.UNKNOWN)
        self.assertIn("RuntimeError", group["checks"][0]["detail"])


class FakeEnvironment(unittest.TestCase):
    """Sonde HTTP, socket HAProxy et certificat servi, contre de faux serveurs locaux."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.seen = []

        seen = cls.seen

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (nom imposé par http.server)
                seen.append(self.headers.get("Host"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        cls.plain = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.plain.serve_forever, daemon=True).start()

        cls.have_openssl = shutil.which("openssl") is not None
        if cls.have_openssl:
            cls.key, cls.crt = f"{cls.tmp}/k.pem", f"{cls.tmp}/c.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", cls.key,
                            "-out", cls.crt, "-days", "30", "-subj", "/CN=test"], check=True, capture_output=True)
            cls.tls = http.server.HTTPServer(("127.0.0.1", 0), Handler)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cls.crt, cls.key)
            cls.tls.socket = ctx.wrap_socket(cls.tls.socket, server_side=True)
            threading.Thread(target=cls.tls.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.plain.shutdown()
        if cls.have_openssl:
            cls.tls.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_private_probe_reaches_the_app_with_the_right_host_header(self):
        site = {"host": "whoami.x.fr", "kind": "priv", "port": self.plain.server_port, "probe_path": "/"}
        code, millis = st.http_probe(site)
        self.assertEqual(code, 200)
        self.assertGreaterEqual(millis, 0)
        self.assertIn("whoami.x.fr", self.seen)

    def test_probe_of_a_closed_port_raises(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with self.assertRaises(OSError):
            st.http_probe({"host": "a.x.fr", "kind": "priv", "port": port}, timeout=2)

    @unittest.skipUnless(shutil.which("openssl"), "openssl absent")
    def test_public_probe_goes_through_tls_with_sni_and_host(self):
        with mock.patch.object(st, "HTTPS_PORT", self.tls.server_port):
            code, _ = st.http_probe({"host": "www.x.fr", "kind": "pub", "port": 8081})
        self.assertEqual(code, 200)
        self.assertIn("www.x.fr", self.seen)

    @unittest.skipUnless(shutil.which("openssl"), "openssl absent")
    def test_served_fingerprint_matches_the_certificate_on_disk(self):
        pem = st.first_pem(Path(self.crt).read_text())
        disk = __import__("hashlib").sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        with mock.patch.object(st, "HTTPS_PORT", self.tls.server_port):
            self.assertEqual(st.served_fingerprint("x.fr"), disk)

    @unittest.skipUnless(shutil.which("openssl"), "openssl absent")
    def test_certificate_check_end_to_end(self):
        live = Path(self.tmp) / "live" / "x.fr"
        live.mkdir(parents=True)
        shutil.copy(self.crt, live / "cert.pem")
        cfg = {"domain": "x.fr", "acme_staging": False}
        with mock.patch.object(st, "LETSENCRYPT_LIVE", str(live.parent)), \
                mock.patch.object(st, "HTTPS_PORT", self.tls.server_port):
            rows = {r["name"]: r for r in st.check_certificate(cfg, {})}
        self.assertEqual(rows["Certificat wildcard"]["level"], st.OK)         # émis pour 30 jours : 29 restants
        self.assertIn("29 jours", rows["Certificat wildcard"]["detail"])
        self.assertEqual(rows["Certificat servi par HAProxy"]["level"], st.OK)
        self.assertEqual(rows["Autorité d'émission"]["level"], st.WARN)        # auto-signé : pas Let's Encrypt

    @unittest.skipUnless(shutil.which("openssl"), "openssl absent")
    def test_certificate_check_detects_a_stale_served_certificate(self):
        other_key, other_crt = f"{self.tmp}/k2.pem", f"{self.tmp}/c2.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", other_key,
                        "-out", other_crt, "-days", "90", "-subj", "/CN=autre"], check=True, capture_output=True)
        live = Path(self.tmp) / "live2" / "x.fr"
        live.mkdir(parents=True)
        shutil.copy(other_crt, live / "cert.pem")          # le disque a un autre certificat que celui servi
        cfg = {"domain": "x.fr", "acme_staging": False}
        with mock.patch.object(st, "LETSENCRYPT_LIVE", str(live.parent)), \
                mock.patch.object(st, "HTTPS_PORT", self.tls.server_port):
            rows = {r["name"]: r for r in st.check_certificate(cfg, {})}
        self.assertEqual(rows["Certificat servi par HAProxy"]["level"], st.CRIT)

    def test_haproxy_admin_socket(self):
        path = f"{self.tmp}/admin.sock"
        csv_text = ("# pxname,svname,qcur,status,lastchg,check_status,req_tot,hrsp_5xx,dreq,\n"
                    "be_pub_www,www,0,UP,120,L4OK,,,,\n")

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                assert self.request.recv(100) == b"show stat\n"
                self.request.sendall(csv_text.encode())

        server = socketserver.UnixStreamServer(path, Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            rows = st.haproxy_show_stat(path)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(rows[0]["status"], "UP")

    def test_full_run_writes_valid_files_and_exits_zero(self):
        cfg = {"domain": "x.fr", "acme_staging": True, "stack_user": "stack", "stack_uid": 1500,
               "stale_after": 900, "smart_expected": False, "system_units": ["ssh.service"],
               "app_units": [], "sites": [], "backup": {"repos": [], "max_age_hours": 36},
               "listen_allowed": [22, 80, 443, 8443], "disk_paths": ["/"]}
        cfg_path = f"{self.tmp}/config.json"
        Path(cfg_path).write_text(json.dumps(cfg))
        out, state = f"{self.tmp}/www", f"{self.tmp}/state"
        os.makedirs(out)
        # aucune commande système : tout doit finir « inconnu » ou « problème », jamais en exception
        with mock.patch.object(st, "run", return_value=(None, "", "commande absente")), \
                contextlib.redirect_stdout(io.StringIO()):
            code = st.main(["--config", cfg_path, "--output-dir", out, "--state-dir", state])
        self.assertEqual(code, 0)
        report = json.loads(Path(out, "status.json").read_text())
        self.assertIn(report["overall"], (st.CRIT, st.WARN, st.UNKNOWN))
        self.assertEqual({g["id"] for g in report["groups"]}, {g[0] for g in st.GROUPS})
        self.assertIn("<!doctype html>", Path(out, "index.html").read_text())
        self.assertEqual(oct(os.stat(Path(out, "index.html")).st_mode & 0o777), "0o644")

    def test_unreadable_config_exits_two(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(st.main(["--config", f"{self.tmp}/absent.json", "--text"]), 2)


if __name__ == "__main__":
    unittest.main()
