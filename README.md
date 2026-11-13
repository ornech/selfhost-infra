# selfhost-infra

Playbook Ansible rejouable : socle Debian durci, Docker, et une stack exposée
(Caddy + CrowdSec + Authelia). Cible 1 : VM ARM64 sur la Freebox Delta (tests).
Cible 2 : serveur physique en DMZ derrière OPNsense (production).

## Ce que fait le playbook

| Rôle | Contenu |
|---|---|
| `base` | mises à jour, compte admin par clé, sshd durci, unattended-upgrades, sysctl, pare-feu nftables (INPUT) |
| `docker` | Docker CE (dépôt officiel, arch auto), `daemon.json` durci |
| `stack` | Caddy (wildcard DNS-01, en-têtes, bouncer CrowdSec), CrowdSec, Authelia (2FA), app publique + service protégé de démo |

Seuls **80/443 (TCP) et 443 (UDP, HTTP/3)** sont publiés. Les backends sont sur un
réseau Docker `internal` : ils n'ont **aucun accès sortant** à Internet.

## Démarrage

```bash
pip install ansible-core
ansible-galaxy collection install -r requirements.yml

# Secrets
echo 'un-mot-de-passe-long' > .vault_pass && chmod 600 .vault_pass
cp group_vars/all/vault.yml.example group_vars/all/vault.yml
$EDITOR group_vars/all/vault.yml
ansible-vault encrypt group_vars/all/vault.yml

# Variables non secrètes : domaine, clé SSH, CIDR d'admin, fournisseur DNS
$EDITOR group_vars/all/main.yml inventory/hosts.yml

ansible-playbook site.yml --check --diff   # à blanc
ansible-playbook site.yml                  # pour de vrai
```

Après le premier passage, connecte-toi avec `admin_user` et retire l'utilisateur
initial de l'inventaire.

## Côté Freebox / DNS

- DNS : `A` pour `example.fr` et `*.example.fr` → IPv4 full-stack. **Pas d'AAAA** tant que
  la Freebox fait routeur (pare-feu IPv6 activé).
- Freebox OS : bail DHCP statique pour la VM, redirection TCP 80, TCP 443, UDP 443 → VM.
- Token DNS : limité à la seule zone, droit d'édition DNS uniquement.
- CAA : `0 issue "letsencrypt.org"`.

## Points d'attention

- **RAM de la VM** : la compilation de Caddy (Go) sur 1 Go peut échouer. Si c'est le cas,
  construire l'image ailleurs (CI multi-arch vers un registre) et remplacer `build:` par `image:`.
- **Images multi-arch** : ta propre application doit exister en `linux/arm64` pour la VM.
- **Ports Docker** : ils contournent la chaîne INPUT nftables. Ne jamais publier d'autre port
  dans `compose.yml` ; la protection des conteneurs passe par l'absence de publication.
- **Authelia en test** : les codes d'enrôlement TOTP arrivent dans
  `docker compose exec authelia cat /config/notification.txt`. En prod : notifier SMTP.
- **Versions** : épinglées dans `group_vars/all/main.yml`. Mise à jour = changement de
  version + PR + CI verte + `ansible-playbook site.yml`.

## Passage en production

1. Ajouter l'hôte physique dans `inventory/hosts.yml`, mettre à jour `admin_cidrs_v4`.
2. `ansible-playbook site.yml`.
3. Basculer les redirections de port vers la nouvelle IP (DMZ OPNsense).
4. Arrêter la VM Freebox.

## Vérifications externes (depuis la 4G)

```bash
nmap -Pn -p- <IPv4>           # seuls 80/443 doivent répondre
nmap -6 -Pn <IPv6 de la VM>   # rien ne doit répondre
testssl.sh https://www.example.fr
curl -sI https://inconnu.example.fr   # connexion fermée (abort)
```
