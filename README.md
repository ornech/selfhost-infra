# selfhost-infra

Playbook Ansible rejouable : socle Debian 13 durci, **Podman rootless + Quadlet**, et une stack
exposée (Caddy + CrowdSec + Authelia). Cible 1 : VM ARM64 sur la Freebox Delta (tests).
Cible 2 : serveur physique en DMZ derrière OPNsense (production).

## Ce que fait le playbook

| Rôle | Contenu |
|---|---|
| `base` | mises à jour, compte admin par clé, sshd durci, unattended-upgrades, sysctl, pare-feu nftables (INPUT) |
| `podman` | Podman 5 (paquets Debian), utilisateur `stack` sans shell, linger systemd, ports 80/443 autorisés en rootless |
| `stack` | secrets Podman, construction de Caddy, unités Quadlet (systemd `--user`) : Caddy, CrowdSec, Authelia, app publique + service protégé de démo |

Seuls **80/443 (TCP) et 443 (UDP, HTTP/3)** sont publiés. Les backends sont sur un
réseau Podman `Internal=true` : ils n'ont **aucun accès sortant** à Internet.

### Pourquoi rootless + Quadlet

- Pas de démon root : une évasion de conteneur donne l'UID `stack`, pas root.
- Les conteneurs sont des services systemd (`systemctl --user`) : redémarrage, démarrage
  au boot, journaux dans journald.
- Les ports publiés passent par `pasta` (processus utilisateur) : ils traversent la chaîne
  INPUT nftables, qui protège donc réellement ce qui est exposé. pasta conserve l'IP
  source, ce dont CrowdSec a besoin.
- Secrets dans `podman secret` (injectés en variable ou en fichier), jamais dans les
  fichiers de configuration.

### Commandes utiles sur l'hôte

```bash
sudo -u stack XDG_RUNTIME_DIR=/run/user/$(id -u stack) systemctl --user status caddy
sudo -u stack XDG_RUNTIME_DIR=/run/user/$(id -u stack) podman ps
sudo -u stack XDG_RUNTIME_DIR=/run/user/$(id -u stack) journalctl --user -u crowdsec -f
sudo -u stack XDG_RUNTIME_DIR=/run/user/$(id -u stack) podman exec crowdsec cscli decisions list
```

## Démarrage

```bash
pip install ansible-core
ansible-galaxy collection install -r requirements.yml   # containers.podman, ansible.posix...

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
  construire l'image ailleurs (CI multi-arch vers un registre) et remplacer la tâche de build par un pull.
- **Images multi-arch** : ta propre application doit exister en `linux/arm64` pour la VM.
- **`ip_unprivileged_port_start=80`** : n'importe quel utilisateur local peut écouter sur
  80+. Acceptable sur un hôte dédié ; à proscrire sur une machine partagée.
- **Rotation d'un secret** : modifier le vault et rejouer. Le fichier source change, le
  secret Podman est recréé (`--replace`) et les services redémarrent.
- **Authelia en test** : les codes d'enrôlement TOTP arrivent dans
  `~stack/stack/authelia/notification.txt`. En prod : notifier SMTP.
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
