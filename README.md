# selfhost-infra

Playbook Ansible rejouable pour auto-héberger des applications web derrière une
Freebox (IPv4 full-stack). Chaque choix est relié à une leçon documentée dans
[DOCTRINE.md](DOCTRINE.md).

## Architecture

```
Internet ──► Freebox (redirection 80/443) ──► hôte Debian 13
                                               │
   nftables ── table crowdsec (IP bannies, noyau) ── table host_fw (politique DROP)
                                               │
   HAProxy (paquet Debian, chroot, utilisateur haproxy)
     :80/:443  fe_public  ── sous-domaines publics, limitation de débit
     :8443     fe_private ── sous-domaines privés, réseaux d'admin seulement
                                               │  127.0.0.1 uniquement
   Podman rootless (utilisateur "stack", unités Quadlet / systemd --user)
     app-pub-www  :8081      app-priv-whoami :8082   ...

   certbot (paquet Debian) ── DNS-01 ── certificat wildcard ── hook ──► HAProxy
   CrowdSec (dépôt officiel signé) ◄── /var/log/haproxy.log, /var/log/auth.log
```

| Rôle | Contenu |
|---|---|
| `base` | compte admin par clé, sshd durci, nftables, sysctl, unattended-upgrades, rsyslog |
| `edge` | HAProxy, certbot + plugin DNS, CrowdSec + bouncer nftables |
| `podman` | Podman 5, utilisateur `stack` sans shell, linger systemd |
| `apps` | contrôle des empreintes d'images, secrets Podman, unités Quadlet |

### Pourquoi HAProxy et CrowdSec sur l'hôte, et pas en conteneur

En Podman rootless, un conteneur attaché à un réseau personnalisé voit l'IP
interne du relais `rootlessport`, pas celle du client. Le mode qui la préserve
est expérimental et absent de Debian 13. Un proxy en conteneur rendrait donc
CrowdSec aveugle. Sur l'hôte, HAProxy voit les vraies IP, s'isole lui-même
(chroot + abandon des privilèges) et reçoit les correctifs de sécurité Debian.

## Prérequis

**Debian 13 (trixie)** sur la cible, et ansible-core ≥ 2.15. Debian 12 ne
convient pas : Podman 4.3 (Quadlet n'apparaît qu'en 4.4), ansible-core 2.14
(pas de `deb822_repository`), et sa maintenance de sécurité régulière est
terminée (passage en LTS). Le playbook vérifie ces versions avant d'agir.

Migration d'une Debian 12 en place :

```bash
sudo apt update && sudo apt full-upgrade
sudo sed -i 's/bookworm/trixie/g' /etc/apt/sources.list /etc/apt/sources.list.d/*.list 2>/dev/null
sudo apt update
sudo apt upgrade --without-new-pkgs
sudo apt full-upgrade
sudo reboot
cat /etc/debian_version     # 13.x attendu
```

## Mise en route

Aucun éditeur à ouvrir : chaque commande se colle seule.

```bash
cd ~/selfhost-infra
sudo apt install ansible skopeo python3-yaml git openssl
```

```bash
scripts/init.sh
```

Le script pose les questions (domaine, e-mail, compte admin, fichier de clé
SSH, plugin DNS et ses identifiants en saisie masquée), puis :
génère `.vault_pass` (aléatoire), renseigne `group_vars/all/main.yml`, crée et
chiffre `group_vars/all/vault.yml`, épingle les images par empreinte.
Il peut être relancé : un vault déjà chiffré n'est pas écrasé.

```bash
git diff group_vars/all/main.yml               # relire ce qui a changé
ansible-playbook site.yml --check --diff -K    # à blanc
```

```bash
ansible-playbook site.yml -K
```

Modifier un secret plus tard : `ansible-vault edit group_vars/all/vault.yml`
(ouvre l'éditeur défini par `$EDITOR`, à lancer seule).

### Avant le premier passage : ne pas se couper l'accès

Le rôle `base` interdit la connexion SSH par mot de passe. Avant de le jouer :

- vérifier que ta clé publique est dans `~/.ssh/authorized_keys` du compte
  indiqué en `ansible_user` (sinon il ne restera que la console de Freebox OS) ;
- vérifier que le réseau depuis lequel tu te connectes est bien dans
  `admin_cidrs_v4` (`ip -4 addr` sur ton poste) ;
- garder une session SSH ouverte pendant le premier passage, et en tester une
  **nouvelle** avant de fermer l'ancienne.

`acme_staging: true` par défaut : le certificat vient de l'autorité de **test**
de Let's Encrypt (avertissement navigateur, pas de limite de débit). Une fois la
VM validée, passer à `false` et rejouer : le certificat est réémis en production.

## Freebox et DNS

- DNS : `A` pour `example.fr` et `*.example.fr` → IPv4 full-stack. **Pas d'AAAA**
  tant que la Freebox fait routeur (son pare-feu IPv6 doit rester activé).
- `CAA 0 issue "letsencrypt.org"`.
- Freebox OS : bail DHCP statique, redirection **TCP 80 et 443 uniquement**.
  Le port 8443 n'est **pas** redirigé : il n'est joignable que depuis le LAN et le VPN.
- Clé d'API DNS limitée à la seule zone (OVH : `GET/POST/DELETE /domain/zone/<domaine>/*`).

## Ajouter une application

Dans `group_vars/all/main.yml` :

```yaml
images:
  monapp: "ghcr.io/moi/monapp:1.2.0@sha256:..."
public_hosts:            # ou private_hosts
  - name: app            # -> app.example.fr
    image: monapp
    port: 8083           # 127.0.0.1:8083 sur l'hôte
    container_port: 3000
    secrets: [app_db_password]   # défini dans vault_app_secrets
    env: { NODE_ENV: production }
    volumes: ["monapp-data:/data"]
```

L'image doit exister pour l'architecture de la cible (`arm64` sur la VM Freebox).

## Exploitation

```bash
# Applications
sudo -u stack XDG_RUNTIME_DIR=/run/user/$(id -u stack) systemctl --user status 'app-*'
sudo -u stack XDG_RUNTIME_DIR=/run/user/$(id -u stack) podman ps
# Périmètre
sudo cscli decisions list            # IP bannies
sudo cscli metrics                   # lignes de journaux lues / parsées
sudo nft list table inet host_fw
echo "show stat" | sudo socat stdio /run/haproxy/admin.sock | cut -d, -f1,2,18
sudo certbot certificates
```

## Vérifications externes (depuis la 4G)

```bash
nmap -Pn -p- <IPv4>                  # seuls 80 et 443 doivent répondre
nmap -6 -Pn <IPv6 de l'hôte>         # rien ne doit répondre
testssl.sh https://www.example.fr
curl -skI https://inconnu.example.fr # connexion coupée sans réponse
```

## Ce qui a été vérifié avant publication

- `ansible-lint` (profil production), `--syntax-check`, `yamllint`.
- `haproxy -c` sur la configuration générée, puis HAProxy lancé avec des
  backends factices : 200 sur `www`, 301 apex → `www` avec conservation de
  l'URI, connexion coupée sur un sous-domaine inconnu, 403 sur le frontal privé
  hors réseau d'admin, en-têtes de sécurité présents et `Server` absent,
  TLS 1.1 refusé, 429 au-delà de 100 requêtes / 10 s.
- `nft -c` sur les règles générées ; générateur Quadlet en mode `-dryrun`.

**Non vérifié** (accès réseau indisponible pendant la rédaction) : installation
réelle des paquets sur Debian 13, émission du certificat, dépôt CrowdSec
`any/any`. Le premier passage sur la VM fait office de recette.

## Passage en production

1. Ajouter l'hôte physique dans `inventory/hosts.yml`, ajuster `admin_cidrs_v4`.
2. `acme_staging: false`, puis `ansible-playbook site.yml`.
3. Basculer la redirection de la Freebox vers la nouvelle IP.
4. Arrêter la VM.
