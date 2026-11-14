#!/usr/bin/python3
# Géré par Ansible (rôle status) : ne pas modifier sur l'hôte.
"""Relevé d'état de l'hôte : écrit une page HTML et un JSON statiques.

Lecture seule. Le script ne modifie rien sur l'hôte en dehors de son dossier
de sortie, de son cache et du cache de restic. Il tourne en root (minuterie
systemd) parce que les contrôles lisent des sockets et des fichiers réservés à
root (nftables, CrowdSec, restic, certificats). Il n'écoute sur AUCUN port : la
page est servie par un conteneur nginx sans privilège qui ne voit que le
dossier de sortie.

Chaque contrôle est isolé : s'il échoue, il apparaît « inconnu » sur la page
et les autres continuent. Un contrôle qui ne peut pas répondre ne doit jamais
passer pour un succès.

Usage :
  selfhost-status          relevé complet ; écrit index.html et status.json
  selfhost-status --text   relevé complet affiché dans le terminal, rien d'écrit
"""
import argparse
import concurrent.futures
import csv
import datetime
import hashlib
import html
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import time

CONFIG_PATH = "/etc/selfhost-status/config.json"
OUTPUT_DIR = "/srv/status/www"
STATE_DIR = "/var/lib/selfhost-status"
HAPROXY_SOCKET = "/run/haproxy/admin.sock"
HTTPS_PORT = 443                      # frontal public de HAProxy (les tests visent un faux serveur)
LETSENCRYPT_LIVE = "/etc/letsencrypt/live"
RESTIC_DIR = "/etc/selfhost-backup"

OK, INFO, WARN, CRIT, UNKNOWN = "ok", "info", "warn", "crit", "unknown"
SEVERITY = {OK: 0, INFO: 0, UNKNOWN: 1, WARN: 2, CRIT: 3}
LABELS = {
    OK: ("✓", "OK"),
    INFO: ("i", "Info"),
    WARN: ("▲", "Attention"),
    CRIT: ("✖", "Problème"),
    UNKNOWN: ("?", "Inconnu"),
}


# ---------------------------------------------------------------- utilitaires
def chk(name, level, detail="", url=None):
    row = {"name": name, "level": level, "detail": detail}
    if url:
        row["url"] = url
    return row


def worst(*levels):
    """Le niveau le plus grave ; à gravité égale, le premier."""
    best = levels[0]
    for level in levels[1:]:
        if SEVERITY[level] > SEVERITY[best]:
            best = level
    return best


def run(argv, timeout=15, env_extra=None, stdin=None):
    """Lance une commande ; renvoie (code, sortie, erreurs), code None si impossible."""
    env = {"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
    if env_extra:
        env.update(env_extra)
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", input=stdin,
            timeout=timeout, env=env, cwd="/", check=False,
        )
    except FileNotFoundError:
        return None, "", "commande absente"
    except subprocess.TimeoutExpired:
        return None, "", "délai dépassé"
    return done.returncode, done.stdout, done.stderr


def user_systemctl(cfg, *args, timeout=15):
    """systemctl --user, dans la session de l'utilisateur des applications (rootless)."""
    return run(
        ["runuser", "-u", cfg["stack_user"], "--", "env",
         f"XDG_RUNTIME_DIR=/run/user/{cfg['stack_uid']}", "systemctl", "--user", *args],
        timeout=timeout,
    )


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h {minutes % 60:02d} min"
    return f"{hours // 24} j {hours % 24} h"


def fmt_bytes(size):
    for unit, div in (("To", 1e12), ("Go", 1e9), ("Mo", 1e6)):
        if size >= div:
            return f"{size / div:.1f} {unit}"
    return f"{size / 1e3:.0f} ko"


def parse_iso(text):
    """RFC 3339 -> datetime avec fuseau.

    restic écrit des nanosecondes ; Python 3.10 ne lit que 3 ou 6 chiffres.
    """
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    match = re.match(r"^(.*?T\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)$", text)
    if not match:
        raise ValueError(f"date illisible : {text!r}")
    base, frac, zone = match.groups()
    frac = (frac or "0")[:6].ljust(6, "0")
    return datetime.datetime.fromisoformat(f"{base}.{frac}{zone}")


def cached(state, key, ttl, fetch, keep=lambda value: True):
    """Mémorise un résultat brut `ttl` secondes (contrôles lents ou distants).

    Seuls les résultats que `keep` accepte sont mémorisés : un échec est
    retenté au relevé suivant.
    """
    now = time.time()
    entry = state.get(key)
    if entry and now - entry["t"] < ttl:
        return entry["v"]
    value = fetch()
    if keep(value):
        state[key] = {"t": now, "v": value}
    return value


# ---------------------------------------------------------------- systemd
def parse_show(text):
    """Sortie de `systemctl show a b c` : un bloc Clé=Valeur par unité, séparés par une ligne vide."""
    blocks, current = [], {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, _, value = line.partition("=")
        current[key] = value
    if current:
        blocks.append(current)
    return blocks


def unit_row(name, props, restart_warn=5):
    if props.get("LoadState") == "not-found":
        return chk(name, CRIT, "unité absente (rôle non joué ?)")
    active, sub = props.get("ActiveState", "?"), props.get("SubState", "?")
    restarts = int(props.get("NRestarts") or 0)
    if active == "active":
        if restarts >= restart_warn:
            return chk(name, WARN, f"actif ({sub}) mais redémarré {restarts} fois : boucle de redémarrage ?")
        return chk(name, OK, f"actif ({sub})")
    if active in ("activating", "reloading"):
        return chk(name, WARN, f"démarrage en cours ({sub})")
    if active == "failed":
        return chk(name, CRIT, f"en échec ({props.get('Result', '?')})")
    return chk(name, CRIT, f"arrêté ({active}/{sub})")


def show_units(units, runner, props="Id,LoadState,ActiveState,SubState,Result,NRestarts"):
    """Renvoie {unité: propriétés} ou None si systemctl ne répond pas."""
    code, out, _ = runner(["show", *units, "-p", props, "--no-pager"])
    if code != 0 or not out.strip():
        return None
    blocks = parse_show(out)
    if len(blocks) != len(units):
        return None
    return dict(zip(units, blocks))


def check_services(cfg, state):
    units = list(cfg["system_units"])
    if os.path.exists("/proc/mdstat") and parse_mdstat(read_text("/proc/mdstat")):
        units.append("mdmonitor.service")
    found = show_units(units, lambda args: run(["systemctl", *args]))
    if found is None:
        return [chk("Services du système", UNKNOWN, "systemctl ne répond pas")]
    return [unit_row(unit, found[unit]) for unit in units]


def check_apps(cfg, state):
    declared = cfg["app_units"]
    if not declared:
        return [chk("Applications", INFO, "aucune application déclarée")]
    units = [item["unit"] for item in declared]
    found = show_units(units, lambda args: user_systemctl(cfg, *args))
    if found is None:
        return [chk("Applications", CRIT,
                    f"session systemd de l'utilisateur « {cfg['stack_user']} » injoignable "
                    "(linger actif ? rôle podman joué ?)")]
    rows = []
    for item in declared:
        row = unit_row(item["unit"], found[item["unit"]])
        row["name"] = item["label"]
        row["detail"] = f"{item['role']} · {row['detail']}"
        rows.append(row)
    return rows


# ---------------------------------------------------------------- HAProxy et sites
def parse_haproxy_stat(text):
    """`show stat` : CSV dont la première ligne (« # pxname,svname,... ») nomme les colonnes."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#"):
        return []
    header = lines[0].lstrip("# ").split(",")
    return [dict(zip(header, record)) for record in csv.reader(lines[1:])]


def haproxy_show_stat(path=HAPROXY_SOCKET, timeout=5):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall(b"show stat\n")
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    finally:
        sock.close()
    return parse_haproxy_stat(b"".join(chunks).decode("utf-8", "replace"))


def http_probe(site, timeout=5):
    """GET sur le site. Public : à travers HAProxy (TLS, aiguillage, application).

    Privé : directement sur l'application, car le frontal privé refuse 127.0.0.1
    (il n'accepte que les réseaux d'administration).
    Renvoie (code HTTP, durée en ms) ; lève OSError si la connexion échoue.
    """
    start = time.monotonic()
    if site["kind"] == "pub":
        port = HTTPS_PORT
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # on teste l'aiguillage, pas la chaîne (voir « Certificat »)
    else:
        port, ctx = site["port"], None
    raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock = ctx.wrap_socket(raw, server_hostname=site["host"]) if ctx else raw
        conn = http.client.HTTPConnection(site["host"], timeout=timeout)
        conn.sock = sock
        conn.request("GET", site.get("probe_path") or "/",
                     headers={"User-Agent": "selfhost-status", "Connection": "close"})
        response = conn.getresponse()
        response.read(4096)
        code = response.status
    finally:
        raw.close()
    return code, int((time.monotonic() - start) * 1000)


def probe_level(code, extra_ok):
    if code < 400 or code in extra_ok:
        return OK
    return WARN if code < 500 else CRIT


def site_row(site, servers, stats_error, probe):
    """Une ligne par site : état du backend HAProxy + réponse HTTP."""
    levels, parts = [], []
    server = servers.get((site["backend"], site["server"]))
    if stats_error:
        levels.append(UNKNOWN)
        parts.append(f"HAProxy : {stats_error}")
    elif server is None:
        levels.append(WARN)
        parts.append("HAProxy : backend inconnu (configuration non rechargée ?)")
    else:
        status = server.get("status", "?")
        since = fmt_duration(int(server.get("lastchg") or 0))
        if status == "UP":
            levels.append(OK)
            parts.append(f"HAProxy UP depuis {since}")
        elif status.startswith("DOWN"):
            levels.append(CRIT)
            parts.append(f"HAProxy DOWN depuis {since} ({server.get('check_status', '?')})")
        else:
            levels.append(WARN)
            parts.append(f"HAProxy {status}")
    if isinstance(probe, Exception):
        levels.append(CRIT)
        parts.append(f"HTTP : connexion impossible ({type(probe).__name__})")
    else:
        code, millis = probe
        levels.append(probe_level(code, site.get("probe_ok", [])))
        parts.append(f"HTTP {code} en {millis} ms")
    kind = "public" if site["kind"] == "pub" else "privé"
    return chk(f"{site['host']} ({kind})", worst(*levels), " · ".join(parts), site.get("url"))


def check_sites(cfg, state):
    sites = cfg["sites"]
    if not sites:
        return [chk("Sites", INFO, "aucun site déclaré")]
    stats, stats_error = [], None
    try:
        stats = haproxy_show_stat()
    except OSError as exc:
        stats_error = f"socket d'administration injoignable ({type(exc).__name__})"
    servers = {(r.get("pxname"), r.get("svname")): r for r in stats}

    def probe(site):
        try:
            return http_probe(site)
        except (OSError, http.client.HTTPException) as exc:
            return exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        probes = list(pool.map(probe, sites))
    rows = [site_row(s, servers, stats_error, p) for s, p in zip(sites, probes)]
    if not stats_error:               # sinon chaque ligne ci-dessus le signale déjà
        rows.append(traffic_row(stats))
    return rows


def traffic_row(stats):
    frontends = [r for r in stats if r.get("svname") == "FRONTEND"]
    if not frontends:
        return chk("Trafic HAProxy", UNKNOWN, "aucune statistique")
    total = sum(int(r.get("req_tot") or 0) for r in frontends)
    errors = sum(int(r.get("hrsp_5xx") or 0) for r in frontends)
    denied = sum(int(r.get("dreq") or 0) for r in frontends)
    detail = (f"{total} requêtes depuis le dernier rechargement de HAProxy, dont {errors} en 5xx ; "
              f"{denied} refusées (limitation de débit, réseau non autorisé)")
    level = WARN if total >= 20 and errors / total > 0.05 else INFO
    return chk("Trafic HAProxy", level, detail)


# ---------------------------------------------------------------- certificat
def first_pem(text):
    match = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S)
    return match.group(0) if match else None


def parse_openssl_cert(text):
    info = {}
    for line in text.splitlines():
        if line.startswith("notAfter="):
            when = datetime.datetime.strptime(line[len("notAfter="):].strip(), "%b %d %H:%M:%S %Y %Z")
            info["not_after"] = when.replace(tzinfo=datetime.timezone.utc)
        elif line.startswith("issuer="):
            info["issuer"] = line[len("issuer="):].strip()
    return info


def served_fingerprint(hostname, timeout=5):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", HTTPS_PORT), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=hostname) as tls:
            return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()


def cert_days_level(days):
    if days < 7:
        return CRIT
    if days < 21:        # certbot renouvelle à 30 jours : en dessous de 21, le renouvellement échoue
        return WARN
    return OK


def check_certificate(cfg, state):
    domain = cfg["domain"]
    live = f"{LETSENCRYPT_LIVE}/{domain}/cert.pem"
    try:
        pem = first_pem(read_text(live))
    except OSError:
        return [chk("Certificat", CRIT, f"{live} illisible : certificat jamais émis ?")]
    if not pem:
        return [chk("Certificat", CRIT, f"{live} ne contient pas de certificat")]
    code, out, _ = run(["openssl", "x509", "-noout", "-enddate", "-issuer"], timeout=10, stdin=pem)
    if code != 0:
        return [chk("Certificat", UNKNOWN, "openssl ne répond pas")]
    info = parse_openssl_cert(out)
    if "not_after" not in info:
        return [chk("Certificat", UNKNOWN, "date d'expiration illisible")]

    now = datetime.datetime.now(datetime.timezone.utc)
    days = (info["not_after"] - now).days
    rows = [chk("Certificat wildcard", cert_days_level(days),
                f"expire le {info['not_after']:%d/%m/%Y} (dans {days} jours)")]

    issuer = info.get("issuer", "")
    staging = "STAGING" in issuer.upper()
    if staging and not cfg["acme_staging"]:
        rows.append(chk("Autorité d'émission", CRIT,
                        "certificat de TEST alors que acme_staging est false : rejouer le rôle edge"))
    elif staging:
        rows.append(chk("Autorité d'émission", INFO,
                        "autorité de TEST Let's Encrypt (acme_staging: true) : normal pendant la phase VM"))
    elif cfg["acme_staging"]:
        rows.append(chk("Autorité d'émission", WARN,
                        "certificat de production alors que acme_staging est true"))
    elif "Let's Encrypt" in issuer:
        rows.append(chk("Autorité d'émission", OK, "Let's Encrypt (production)"))
    else:
        rows.append(chk("Autorité d'émission", WARN, f"émetteur inattendu : {issuer or 'inconnu'}"))

    # Le certificat renouvelé sur le disque est-il bien celui que HAProxy présente ?
    # (hook de déploiement ou rechargement en échec = l'ancien certificat reste servi)
    try:
        served = served_fingerprint(domain)
        disk = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        if served == disk:
            rows.append(chk("Certificat servi par HAProxy", OK, "identique à celui du disque"))
        else:
            rows.append(chk("Certificat servi par HAProxy", CRIT,
                            "différent de celui du disque : le hook de renouvellement ou le "
                            "rechargement de HAProxy a échoué"))
    except (OSError, ssl.SSLError) as exc:
        rows.append(chk("Certificat servi par HAProxy", CRIT,
                        f"connexion TLS locale impossible ({type(exc).__name__})"))
    return rows


# ---------------------------------------------------------------- sauvegardes
def fetch_snapshots(repo_name):
    """Instantanés d'un dépôt restic, en lecture seule (--no-lock : rien n'est écrit dans le dépôt).

    Aucune sortie d'erreur de restic n'est conservée : elle peut citer l'URL du
    dépôt, qui contient des identifiants.
    """
    code, out, err = run(
        ["restic", "snapshots", "--json", "--no-lock"], timeout=90,
        env_extra={
            "HOME": "/root",
            "RESTIC_CACHE_DIR": "/root/.cache/restic",
            "RESTIC_PASSWORD_FILE": f"{RESTIC_DIR}/password",
            "RESTIC_REPOSITORY_FILE": f"{RESTIC_DIR}/repos/{repo_name}",
        })
    if code != 0:
        return {"ok": False, "code": err if code is None else code}
    try:
        snaps = json.loads(out) or []
        last = max((parse_iso(s["time"]).timestamp() for s in snaps), default=None)
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "code": "sortie illisible"}
    return {"ok": True, "count": len(snaps), "last": last}


def snapshot_row(repo_name, result, max_age_hours, now):
    name = f"Dépôt « {repo_name} »"
    if not result["ok"]:
        if result["code"] == "commande absente":
            return chk(name, UNKNOWN, "restic n'est pas installé : impossible de lire le dépôt")
        return chk(name, CRIT, f"injoignable ou illisible (restic : {result['code']})")
    if result["last"] is None:
        return chk(name, CRIT, "dépôt vide : aucune sauvegarde n'a encore abouti")
    age = now - result["last"]
    when = datetime.datetime.fromtimestamp(result["last"]).astimezone().strftime("%d/%m %H:%M")
    detail = f"dernier instantané il y a {fmt_duration(age)} ({when}) · {result['count']} instantanés"
    if age > 2 * max_age_hours * 3600:
        level = CRIT
    elif age > max_age_hours * 3600:
        level = WARN
    else:
        level = OK
    return chk(name, level, detail)


def check_backup(cfg, state):
    backup = cfg["backup"]
    if not backup["repos"]:
        return [chk("Sauvegarde", WARN, "non configurée (backup_repositories est vide) : "
                    "aucune donnée n'est sauvegardée")]
    rows, now = [], time.time()
    for name in backup["repos"]:
        result = cached(state, f"restic:{name}", 1800, lambda n=name: fetch_snapshots(n),
                        keep=lambda value: value["ok"])
        rows.append(snapshot_row(name, result, backup["max_age_hours"], now))

    found = show_units(["selfhost-backup.service", "selfhost-backup-check.service"],
                       lambda args: run(["systemctl", *args]), props="Id,LoadState,ActiveState,Result")
    if found is None:
        rows.append(chk("Tâches de sauvegarde", UNKNOWN, "systemctl ne répond pas"))
        return rows
    daily = found["selfhost-backup.service"]
    if daily.get("LoadState") == "not-found":
        rows.append(chk("Dernière sauvegarde (tâche)", CRIT, "unité selfhost-backup absente (rôle backup non joué ?)"))
    elif daily.get("Result") not in (None, "", "success"):
        rows.append(chk("Dernière sauvegarde (tâche)", CRIT, f"en échec ({daily['Result']}) : "
                        "journalctl -u selfhost-backup"))
    else:
        rows.append(chk("Dernière sauvegarde (tâche)", OK, "aucun échec constaté depuis le démarrage"))
    weekly = found["selfhost-backup-check.service"]
    stamp = "/var/lib/systemd/timers/stamp-selfhost-backup-check.timer"
    if weekly.get("LoadState") == "not-found":
        rows.append(chk("Vérification d'intégrité", CRIT,
                        "unité selfhost-backup-check absente (rôle backup non joué ?)"))
    elif weekly.get("Result") not in (None, "", "success"):
        rows.append(chk("Vérification d'intégrité", CRIT, f"dernière vérification en échec ({weekly['Result']}) : "
                        "journalctl -u selfhost-backup-check"))
    elif os.path.exists(stamp):
        age = now - os.stat(stamp).st_mtime
        level = OK if age < 9 * 86400 else WARN
        rows.append(chk("Vérification d'intégrité", level, f"lancée il y a {fmt_duration(age)} (hebdomadaire)"))
    else:
        rows.append(chk("Vérification d'intégrité", INFO, "pas encore exécutée (hebdomadaire)"))
    return rows


# ---------------------------------------------------------------- sécurité
def parse_nft_policy(text, table, chain):
    """Politique d'une chaîne, depuis `nft -j list chain` ; None si la chaîne est absente."""
    for item in json.loads(text).get("nftables", []):
        found = item.get("chain")
        if found and found.get("table") == table and found.get("name") == chain:
            return found.get("policy")
    return None


def parse_proc_net_tcp(text, ipv6=False):
    """Sockets en écoute (état 0A) de /proc/net/tcp[6] : liste de (adresse, port, inode)."""
    sockets = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != "0A":
            continue
        host, port = fields[1].split(":")
        if ipv6:
            words = [struct.pack("<I", int(host[i:i + 8], 16)) for i in range(0, 32, 8)]
            address = socket.inet_ntop(socket.AF_INET6, b"".join(words))
        else:
            address = socket.inet_ntoa(struct.pack("<I", int(host, 16)))
        sockets.append((address, int(port, 16), fields[9]))
    return sockets


def is_loopback(address):
    return address == "::1" or address.startswith("127.") or address.startswith("::ffff:127.")


def process_of_socket(inode):
    """Nom du processus qui détient un socket (root voit tous les /proc/<pid>/fd)."""
    target = f"socket:[{inode}]"
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return "?"
    for pid in pids:
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                if os.readlink(f"/proc/{pid}/fd/{fd}") == target:
                    return read_text(f"/proc/{pid}/comm").strip()
        except OSError:
            continue
    return "?"


def firewall_rows():
    """La politique par défaut de l'hôte est-elle toujours DROP, et le bouncer CrowdSec bloque-t-il ?"""
    rows = []
    code, out, _ = run(["nft", "-j", "list", "chain", "inet", "host_fw", "input"])
    if code != 0:
        rows.append(chk("Pare-feu de l'hôte", CRIT, "table inet host_fw absente ou nft illisible : "
                        "l'hôte n'est peut-être plus filtré"))
    else:
        try:
            policy = parse_nft_policy(out, "host_fw", "input")
        except ValueError:
            policy = None
        if policy == "drop":
            rows.append(chk("Pare-feu de l'hôte", OK, "nftables : politique d'entrée DROP"))
        else:
            rows.append(chk("Pare-feu de l'hôte", CRIT, f"politique d'entrée « {policy} » au lieu de drop"))
    code, out, _ = run(["nft", "list", "tables"])
    if code != 0:
        rows.append(chk("Blocage CrowdSec (noyau)", UNKNOWN, "nft ne répond pas"))
    elif "crowdsec" in out:
        rows.append(chk("Blocage CrowdSec (noyau)", OK, "tables nftables du bouncer présentes"))
    else:
        rows.append(chk("Blocage CrowdSec (noyau)", CRIT, "aucune table crowdsec dans nftables : "
                        "les IP bannies ne sont pas bloquées"))
    return rows


def check_security(cfg, state):
    return firewall_rows() + [
        crowdsec_bouncer_row(),     # le bouncer interroge-t-il bien le moteur ?
        crowdsec_decisions_row(),
        sshd_row(),                 # la configuration EFFECTIVE, pas seulement le fichier déposé
        listening_row(cfg),         # la surface d'écoute réelle
    ]


def crowdsec_bouncer_row():
    code, out, _ = run(["cscli", "bouncers", "list", "-o", "json"], timeout=20)
    if code != 0:
        return chk("Bouncer CrowdSec", UNKNOWN, "cscli ne répond pas")
    try:
        bouncers = json.loads(out) or []
    except ValueError:
        return chk("Bouncer CrowdSec", UNKNOWN, "sortie de cscli illisible")
    if not bouncers:
        return chk("Bouncer CrowdSec", CRIT, "aucun bouncer enregistré : rien n'applique les bannissements")
    now = datetime.datetime.now(datetime.timezone.utc)
    details, level = [], OK
    for bouncer in bouncers:
        name = bouncer.get("name", "?")
        try:
            age = (now - parse_iso(bouncer["last_pull"])).total_seconds()
        except (KeyError, ValueError, TypeError):
            level = worst(level, WARN)
            details.append(f"{name} : jamais interrogé")
            continue
        if bouncer.get("revoked"):
            level = worst(level, CRIT)
            details.append(f"{name} : révoqué")
        elif age > 300:
            level = worst(level, CRIT)
            details.append(f"{name} : dernière interrogation il y a {fmt_duration(age)}")
        else:
            details.append(f"{name} : interrogé il y a {fmt_duration(age)}")
    return chk("Bouncer CrowdSec", level, " · ".join(details))


def crowdsec_decisions_row():
    code, out, _ = run(["cscli", "decisions", "list", "-o", "json"], timeout=20)
    if code != 0:
        return chk("IP bannies (CrowdSec)", UNKNOWN, "cscli ne répond pas")
    try:
        alerts = json.loads(out) or []
    except ValueError:
        return chk("IP bannies (CrowdSec)", UNKNOWN, "sortie de cscli illisible")
    scenarios = {}
    total = 0
    for alert in alerts:
        decisions = alert.get("decisions") or [alert]
        total += len(decisions)
        scenario = alert.get("scenario") or "?"
        scenarios[scenario] = scenarios.get(scenario, 0) + len(decisions)
    if not total:
        return chk("IP bannies (CrowdSec)", INFO, "aucune décision active")
    top = ", ".join(f"{name} ({count})" for name, count in
                    sorted(scenarios.items(), key=lambda kv: -kv[1])[:3])
    return chk("IP bannies (CrowdSec)", INFO, f"{total} décision(s) locale(s) active(s) · {top}")


SSHD_EXPECTED = {
    "passwordauthentication": "no",
    "kbdinteractiveauthentication": "no",
    "permitrootlogin": "no",
    "pubkeyauthentication": "yes",
    "x11forwarding": "no",
}


def parse_sshd_config(text):
    config = {}
    for line in text.splitlines():
        key, _, value = line.strip().partition(" ")
        if key:
            config.setdefault(key, value.strip())
    return config


def sshd_row():
    code, out, _ = run(["sshd", "-T"], timeout=10)
    if code != 0:
        return chk("Durcissement SSH", UNKNOWN, "sshd -T ne répond pas")
    config = parse_sshd_config(out)
    wrong = [f"{key} = {config.get(key, 'absent')} (attendu : {want})"
             for key, want in SSHD_EXPECTED.items() if config.get(key) != want]
    if wrong:
        return chk("Durcissement SSH", CRIT, "configuration effective non conforme : " + " ; ".join(wrong))
    return chk("Durcissement SSH", OK, "clé uniquement, root interdit (configuration effective de sshd)")


def listening_row(cfg):
    allowed = set(cfg["listen_allowed"])
    sockets = []
    for path, ipv6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            sockets += parse_proc_net_tcp(read_text(path), ipv6)
        except OSError:
            return chk("Ports en écoute", UNKNOWN, f"{path} illisible")
    exposed = [(addr, port, inode) for addr, port, inode in sockets if not is_loopback(addr)]
    unexpected = {}                       # un port écoute souvent en IPv4 ET en IPv6 : on le compte une fois
    for _, port, inode in exposed:
        if port not in allowed:
            unexpected.setdefault(port, inode)
    if unexpected:
        parts = [f"{port} ({process_of_socket(inode)})" for port, inode in sorted(unexpected.items())[:5]]
        return chk("Ports en écoute", WARN, "hors boucle locale, non prévus : " + ", ".join(parts))
    ports = ", ".join(str(p) for p in sorted({port for _, port, _ in exposed}))
    return chk("Ports en écoute", OK, f"hors boucle locale : {ports or 'aucun'} (tous prévus)")


# ---------------------------------------------------------------- stockage
def parse_mdstat(text):
    """Grappes RAID de /proc/mdstat : liste de dicts {name, state, pattern, progress, failed}."""
    arrays, current = [], None
    for line in text.splitlines():
        head = re.match(r"^(md\S+)\s*:\s*(\S+)", line)
        if head:
            current = {"name": head.group(1), "state": head.group(2), "pattern": None,
                       "progress": None, "failed": "(F)" in line}
            arrays.append(current)
            continue
        if current is None:
            continue
        pattern = re.search(r"\[(\d+)/(\d+)\]\s+\[([U_]+)\]", line)
        if pattern:
            current["pattern"] = pattern.group(3)
        progress = re.search(r"(recovery|resync|reshape|check)\s*=\s*([\d.]+)%", line)
        if progress:
            current["progress"] = (progress.group(1), progress.group(2))
    return arrays


def raid_rows():
    try:
        arrays = parse_mdstat(read_text("/proc/mdstat"))
    except OSError:
        return [chk("RAID", INFO, "pas de RAID logiciel sur cette machine")]
    if not arrays:
        return [chk("RAID", INFO, "aucune grappe mdadm (normal sur une VM)")]
    rows = []
    for array in arrays:
        degraded = array["failed"] or (array["pattern"] and "_" in array["pattern"])
        if array["state"] != "active":
            rows.append(chk(f"RAID {array['name']}", CRIT, f"état « {array['state']} »"))
        elif degraded:
            extra = f" · {array['progress'][0]} {array['progress'][1]} %" if array["progress"] else ""
            rows.append(chk(f"RAID {array['name']}", CRIT,
                            f"DÉGRADÉ [{array['pattern']}]{extra} : voir README, « Remplacer un disque »"))
        elif array["progress"]:
            rows.append(chk(f"RAID {array['name']}", WARN,
                            f"{array['progress'][0]} en cours : {array['progress'][1]} %"))
        else:
            rows.append(chk(f"RAID {array['name']}", OK, f"sain [{array['pattern'] or '-'}]"))
    return rows


def smart_rows(cfg, state):
    if not cfg["smart_expected"]:
        return [chk("SMART", INFO, "machine virtuelle : non applicable")]

    def fetch():
        code, out, _ = run(["smartctl", "--scan", "-j"], timeout=20)
        if code is None:
            return {"ok": False}
        devices = []
        for dev in json.loads(out).get("devices", []):
            code, out, _ = run(["smartctl", "-H", "-A", "-j", "-d", dev.get("type", "auto"), dev["name"]],
                               timeout=30)
            if code is None:
                continue
            data = json.loads(out)
            devices.append({
                "name": dev["name"],
                "passed": (data.get("smart_status") or {}).get("passed"),
                "temp": (data.get("temperature") or {}).get("current"),
            })
        return {"ok": True, "devices": devices}

    try:
        result = cached(state, "smart", 3600, fetch, keep=lambda value: value["ok"])
    except (ValueError, KeyError):
        return [chk("SMART", UNKNOWN, "sortie de smartctl illisible")]
    if not result["ok"]:
        return [chk("SMART", UNKNOWN, "smartctl absent (paquet smartmontools)")]
    if not result["devices"]:
        return [chk("SMART", WARN, "aucun disque détecté")]
    rows = []
    for dev in result["devices"]:
        temp = f" · {dev['temp']} °C" if dev["temp"] is not None else ""
        if dev["passed"] is True:
            rows.append(chk(f"SMART {dev['name']}", OK, f"verdict de santé : réussi{temp}"))
        elif dev["passed"] is False:
            rows.append(chk(f"SMART {dev['name']}", CRIT, f"verdict de santé : ÉCHEC{temp}"))
        else:
            rows.append(chk(f"SMART {dev['name']}", UNKNOWN, "verdict indisponible"))
    return rows


def disk_rows(cfg):
    by_device = {}
    for path in cfg["disk_paths"]:
        try:
            device = os.stat(path).st_dev
        except OSError:
            continue
        by_device.setdefault(device, []).append(path)
    rows = []
    for paths in by_device.values():
        usage = shutil.disk_usage(paths[0])
        percent = usage.used * 100 / usage.total if usage.total else 0
        level = CRIT if percent >= 90 else WARN if percent >= 80 else OK
        detail = f"{percent:.0f} % utilisé · {fmt_bytes(usage.free)} libres sur {fmt_bytes(usage.total)}"
        stat = os.statvfs(paths[0])
        if stat.f_files:
            inodes = (stat.f_files - stat.f_ffree) * 100 / stat.f_files
            if inodes >= 90:
                level = worst(level, CRIT)
                detail += f" · inodes {inodes:.0f} %"
        rows.append(chk(f"Disque {', '.join(paths)}", level, detail))
    return rows


def check_storage(cfg, state):
    return raid_rows() + smart_rows(cfg, state) + disk_rows(cfg)


# ---------------------------------------------------------------- hôte et mises à jour
def kernel_version(release):
    match = re.match(r"(\d+(?:\.\d+)*)", release)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def newer_kernel(running, installed):
    """Version du noyau installé le plus récent, de la même variante, s'il est plus récent que celui qui tourne."""
    flavour = running.partition("-")[2]
    candidates = [r for r in installed if r.partition("-")[2] == flavour
                  and kernel_version(r) > kernel_version(running)]
    return max(candidates, key=kernel_version) if candidates else None


def parse_apt_simulation(text):
    total = security = 0
    for line in text.splitlines():
        if line.startswith("Inst "):
            total += 1
            if "security" in line.lower():
                security += 1
    return total, security


def update_rows(state):
    def fetch():
        code, out, _ = run(["apt-get", "-s", "-o", "Debug::NoLocking=true", "dist-upgrade"], timeout=60)
        if code != 0:
            return {"ok": False}
        total, security = parse_apt_simulation(out)
        return {"ok": True, "total": total, "security": security}

    result = cached(state, "apt", 3600, fetch, keep=lambda value: value["ok"])
    rows = []
    if not result["ok"]:
        rows.append(chk("Mises à jour en attente", UNKNOWN, "apt-get ne répond pas"))
    elif result["security"]:
        rows.append(chk("Mises à jour en attente", WARN,
                        f"{result['total']} paquet(s) dont {result['security']} de sécurité : "
                        "unattended-upgrades les applique chaque jour ; si cela dure, "
                        "journalctl -u apt-daily-upgrade"))
    else:
        rows.append(chk("Mises à jour en attente", OK if not result["total"] else INFO,
                        f"{result['total']} paquet(s), aucun de sécurité"))
    now = time.time()
    for label, stamp in (("Listes de paquets", "/var/lib/apt/periodic/update-success-stamp"),
                         ("Mises à jour automatiques", "/var/lib/apt/periodic/unattended-upgrades-stamp")):
        try:
            age = now - os.stat(stamp).st_mtime
        except OSError:
            rows.append(chk(label, INFO, "pas encore exécutées"))
            continue
        level = OK if age < 3 * 86400 else WARN
        rows.append(chk(label, level, f"dernier passage réussi il y a {fmt_duration(age)}"))

    running = os.uname().release
    try:
        installed = [name[len("vmlinuz-"):] for name in os.listdir("/boot") if name.startswith("vmlinuz-")]
    except OSError:
        installed = []
    newer = newer_kernel(running, installed)
    if os.path.exists("/run/reboot-required") or newer:
        why = f"noyau {newer} installé, {running} en cours" if newer else "fichier /run/reboot-required"
        rows.append(chk("Redémarrage", WARN, f"nécessaire ({why})"))
    else:
        rows.append(chk("Redémarrage", OK, f"non nécessaire (noyau {running})"))
    return rows


def failed_units_row(cfg):
    names, errors = [], []
    code, out, _ = run(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"])
    if code == 0:
        names += [line.split()[0] for line in out.splitlines() if line.strip()]
    else:
        errors.append("système")
    code, out, _ = user_systemctl(cfg, "--failed", "--no-legend", "--plain", "--no-pager")
    if code == 0:
        names += [line.split()[0] + " (utilisateur)" for line in out.splitlines() if line.strip()]
    else:
        errors.append("utilisateur")
    if names:
        return chk("Unités systemd en échec", CRIT, ", ".join(names))
    if errors:
        return chk("Unités systemd en échec", UNKNOWN, "systemctl ne répond pas (" + ", ".join(errors) + ")")
    return chk("Unités systemd en échec", OK, "aucune")


def host_rows(cfg, state):
    rows = []
    pretty = "Linux"
    try:
        for line in read_text("/etc/os-release").splitlines():
            if line.startswith("PRETTY_NAME="):
                pretty = line.partition("=")[2].strip('"')
    except OSError:
        pass
    try:
        uptime = fmt_duration(float(read_text("/proc/uptime").split()[0]))
    except (OSError, ValueError):
        uptime = "?"
    rows.append(chk("Hôte", INFO, f"{pretty} · noyau {os.uname().release} · démarré depuis {uptime}"))

    cpus = os.cpu_count() or 1
    load1, load5, load15 = os.getloadavg()
    level = CRIT if load5 > 4 * cpus else WARN if load5 > 2 * cpus else OK
    rows.append(chk("Charge", level, f"{load1:.2f} / {load5:.2f} / {load15:.2f} (1, 5, 15 min) pour {cpus} cœur(s)"))

    try:
        meminfo = {}
        for line in read_text("/proc/meminfo").splitlines():
            key, _, value = line.partition(":")
            meminfo[key] = int(value.split()[0]) * 1024
        free = meminfo["MemAvailable"] * 100 / meminfo["MemTotal"]
        level = CRIT if free < 5 else WARN if free < 10 else OK
        rows.append(chk("Mémoire", level, f"{fmt_bytes(meminfo['MemAvailable'])} disponibles sur "
                        f"{fmt_bytes(meminfo['MemTotal'])} ({free:.0f} %)"))
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        rows.append(chk("Mémoire", UNKNOWN, "/proc/meminfo illisible"))

    code, out, _ = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if code != 0:
        rows.append(chk("Horloge", UNKNOWN, "timedatectl ne répond pas"))
    elif out.strip() == "yes":
        rows.append(chk("Horloge", OK, "synchronisée (NTP)"))
    else:
        rows.append(chk("Horloge", WARN, "non synchronisée : les certificats TLS et les journaux en dépendent"))

    rows += update_rows(state)
    rows.append(failed_units_row(cfg))
    return rows


# ---------------------------------------------------------------- relevé complet
GROUPS = [
    ("sites", "Sites", check_sites),
    ("apps", "Applications (conteneurs)", check_apps),
    ("services", "Services du système", check_services),
    ("certificat", "Certificat TLS", check_certificate),
    ("sauvegardes", "Sauvegardes", check_backup),
    ("securite", "Sécurité", check_security),
    ("stockage", "Stockage", check_storage),
    ("hote", "Hôte et mises à jour", host_rows),
]


def run_group(group, cfg, state):
    gid, title, function = group
    try:
        rows = function(cfg, state)
    except Exception as exc:  # noqa: BLE001 - un contrôle qui casse ne doit pas masquer les autres
        rows = [chk(title, UNKNOWN, f"contrôle en échec : {type(exc).__name__}: {str(exc)[:160]}")]
    return {"id": gid, "title": title, "checks": rows}


def build_report(cfg, state):
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(GROUPS)) as pool:
        groups = list(pool.map(lambda g: run_group(g, cfg, state), GROUPS))
    counts = {level: 0 for level in LABELS}
    for group in groups:
        for row in group["checks"]:
            counts[row["level"]] += 1
    if counts[CRIT]:
        overall = CRIT
    elif counts[WARN]:
        overall = WARN
    elif counts[UNKNOWN]:
        overall = UNKNOWN
    else:
        overall = OK
    now = datetime.datetime.now().astimezone()
    return {
        "domain": cfg["domain"],
        "generated_at": int(now.timestamp()),
        "generated_iso": now.isoformat(timespec="seconds"),
        "stale_after": cfg["stale_after"],
        "overall": overall,
        "counts": counts,
        "groups": groups,
    }


# ---------------------------------------------------------------- rendu
HEADLINES = {
    OK: "Tout est en ordre",
    WARN: "Des points demandent de l'attention",
    CRIT: "Des problèmes demandent une intervention",
    UNKNOWN: "Vérification incomplète : certains contrôles n'ont pas pu répondre",
}


def render_text(report):
    lines = [f"État de {report['domain']} — {report['generated_iso']}",
             f"Global : {HEADLINES[report['overall']]}", ""]
    for group in report["groups"]:
        lines.append(f"[{group['title']}]")
        for row in group["checks"]:
            sym, word = LABELS[row["level"]]
            lines.append(f"  {sym} {word:<9} {row['name']} : {row['detail']}")
        lines.append("")
    return "\n".join(lines)


def render_row(row):
    sym, word = LABELS[row["level"]]
    name = html.escape(row["name"])
    if row.get("url"):
        name = f'<a href="{html.escape(row["url"], quote=True)}" rel="noopener noreferrer">{name}</a>'
    return (f'<li class="row" data-level="{row["level"]}">'
            f'<span class="badge"><span aria-hidden="true">{sym}</span> {word}</span>'
            f'<span class="name">{name}</span>'
            f'<span class="detail">{html.escape(row["detail"])}</span></li>')


def render_html(report):
    counts = report["counts"]
    names = {CRIT: "problème(s)", WARN: "attention(s)", UNKNOWN: "inconnu(s)", OK: "ok", INFO: "info(s)"}
    summary = " · ".join(f"{counts[level]} {names[level]}"
                         for level in (CRIT, WARN, UNKNOWN, OK, INFO) if counts[level])
    todo = [(g["title"], r) for g in report["groups"] for r in g["checks"]
            if r["level"] in (CRIT, WARN, UNKNOWN)]
    todo.sort(key=lambda item: -SEVERITY[item[1]["level"]])
    todo_html = ""
    if todo:
        items = "".join(
            f'<li data-level="{r["level"]}"><strong>{html.escape(title)} · {html.escape(r["name"])}</strong> '
            f'— {html.escape(r["detail"])}</li>' for title, r in todo)
        todo_html = f'<section class="todo"><h2>À traiter</h2><ul>{items}</ul></section>'
    sections = "".join(
        f'<section id="{g["id"]}"><h2>{html.escape(g["title"])}</h2>'
        f'<ul class="rows">{"".join(render_row(r) for r in g["checks"])}</ul></section>'
        for g in report["groups"])
    when = datetime.datetime.fromtimestamp(report["generated_at"]).astimezone().strftime("%d/%m/%Y à %H:%M:%S")
    return f"""<!doctype html>
<html lang="fr" data-generated="{report['generated_at']}" data-stale-after="{report['stale_after']}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'self'; script-src 'self'; img-src data:; base-uri 'none'; form-action 'none'">
<meta http-equiv="refresh" content="60">
<meta name="robots" content="noindex, nofollow">
<meta name="color-scheme" content="light dark">
<title>État · {html.escape(report['domain'])}</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="status.css">
<script src="status.js" defer></script>
</head>
<body>
<main>
<header>
<h1>État de l'infrastructure</h1>
<p class="meta">{html.escape(report['domain'])} · relevé du {when} (<span id="age">à l'instant</span>)</p>
</header>
<p id="stale" class="stale" hidden>Ces données sont périmées : le relevé automatique ne s'exécute plus
(systemctl status selfhost-status.timer). L'état affiché n'est plus fiable.</p>
<section class="overall" data-level="{report['overall']}">
<p class="headline"><span aria-hidden="true">{LABELS[report['overall']][0]}</span> {html.escape(HEADLINES[report['overall']])}</p>
<p class="summary">{html.escape(summary)}</p>
</section>
{todo_html}
{sections}
<footer><p>Page en lecture seule, générée par le playbook selfhost-infra. Données brutes : <a href="status.json">status.json</a>.</p></footer>
</main>
</body>
</html>
"""


def write_atomic(path, content):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def load_state(state_dir):
    try:
        with open(os.path.join(state_dir, "cache.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state_dir, state):
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "cache.json")
    with open(f"{path}.tmp", "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(f"{path}.tmp", path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Relevé d'état de l'hôte (lecture seule).")
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--state-dir", default=STATE_DIR)
    parser.add_argument("--text", action="store_true", help="afficher le relevé, ne rien écrire")
    args = parser.parse_args(argv)

    try:
        with open(args.config, encoding="utf-8") as handle:
            cfg = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"configuration illisible ({args.config}) : {exc}", file=sys.stderr)
        return 2

    state = load_state(args.state_dir)
    report = build_report(cfg, state)
    if args.text:
        print(render_text(report))
        return 0

    write_atomic(os.path.join(args.output_dir, "index.html"), render_html(report))
    write_atomic(os.path.join(args.output_dir, "status.json"), json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    save_state(args.state_dir, state)
    counts = report["counts"]
    print(f"état global : {report['overall']} "
          f"({counts[CRIT]} problème(s), {counts[WARN]} attention(s), {counts[UNKNOWN]} inconnu(s))")
    return 0   # un état rouge n'est pas un échec du relevé


if __name__ == "__main__":
    sys.exit(main())
