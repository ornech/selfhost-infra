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
| `status` | page d'état en lecture seule (relevé périodique → fichiers statiques → nginx sans privilège) |
| `apps` | contrôle des empreintes d'images, secrets Podman, unités Quadlet |
| `storage` | surveillance RAID (mdadm) et SMART ; arrêt si miroir dégradé |
| `backup` | restic : exports, sauvegarde quotidienne multi-dépôts, vérification hebdomadaire |
| `backup_target` | (machine distante, `backup-target.yml`) rest-server en mode ajout seul |

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

Chaque bloc se colle **seul**, dans l'ordre. Aucun éditeur, aucun script :
chaque ligne dit ce qu'elle modifie, et une commande de contrôle suit.

**0. Outils et dépôt**

```bash
sudo apt install ansible skopeo git openssl
cd ~/selfhost-infra && git pull
```

**1. Mot de passe du vault** (aléatoire, ignoré par Git, à sauvegarder hors du serveur)

```bash
openssl rand -base64 32 > .vault_pass
chmod 600 .vault_pass
```

**2. Tes valeurs** (adapter avant de coller)

```bash
DOMAINE=mondomaine.fr
EMAIL=moi@mondomaine.fr
ADMIN=jf
```

**3. Report dans `group_vars/all/main.yml`**

```bash
if [ -n "$DOMAINE" ] && [ -n "$EMAIL" ] && [ -n "$ADMIN" ]; then
  sed -i "s|^domain: .*|domain: $DOMAINE|" group_vars/all/main.yml
  sed -i "s|^acme_email: .*|acme_email: $EMAIL|" group_vars/all/main.yml
  sed -i "s|^admin_user: .*|admin_user: $ADMIN|" group_vars/all/main.yml
else
  echo "ÉCHEC : DOMAINE, EMAIL ou ADMIN est vide : refaire l'étape 2"
fi
CLE=$(grep -m1 '^ssh-' ~/.ssh/authorized_keys)
sed -i "s|^  - \"ssh-ed25519 AAAA...remplace-moi jf@poste\"|  - \"$CLE\"|" group_vars/all/main.yml
grep -nE '^(domain|acme_email|admin_user):|^  - "ssh' group_vars/all/main.yml
```

**4. Jeton d'API Cloudflare** : saisi sans affichage et sans trace dans l'historique.

```bash
read -rsp "Jeton Cloudflare : " CF_TOKEN; echo
```

La commande attend ta saisie : colle le jeton à l'invite, puis Entrée (rien ne
s'affiche). Le dernier mot, `CF_TOKEN`, est le **nom** de la variable, pas la
valeur. Contrôle que la longueur n'est pas nulle (53 pour un jeton `cfut_…`) :

```bash
echo "longueur : ${#CF_TOKEN}"
```

**5. Création et chiffrement du vault**

```bash
if [ -n "$CF_TOKEN" ]; then
cat > group_vars/all/vault.yml <<FIN
---
vault_certbot_dns_credentials: |
  dns_cloudflare_api_token = $CF_TOKEN
vault_app_secrets: {}
FIN
chmod 600 group_vars/all/vault.yml
ansible-vault encrypt group_vars/all/vault.yml
else
  echo "ÉCHEC : jeton vide, le vault n'est pas modifié"
fi
unset CF_TOKEN
head -1 group_vars/all/vault.yml
```

La dernière ligne doit afficher `$ANSIBLE_VAULT;1.1;AES256`.

**6. Épinglage de l'image par empreinte**

```bash
if skopeo inspect --raw docker://docker.io/traefik/whoami:v1.10 > /tmp/manifeste.json && [ -s /tmp/manifeste.json ]; then
  DIGEST=sha256:$(sha256sum /tmp/manifeste.json | cut -d' ' -f1)
  sed -i "s|^  whoami: \"docker.io/traefik/whoami:v1.10\"|  whoami: \"docker.io/traefik/whoami:v1.10@$DIGEST\"|" group_vars/all/main.yml
else
  echo "ÉCHEC : manifeste non lu, rien n'est épinglé"
fi
grep -n '  whoami:' group_vars/all/main.yml
```

L'empreinte est celle du manifeste multi-architecture téléchargé : c'est
exactement ce que Podman vérifiera au moment du pull.

**7. Relecture puis passage à blanc**

```bash
git diff group_vars/all/main.yml
ansible-playbook site.yml --check --diff -K
```

Sur une machine vierge, le passage à blanc s'arrête à « Clés SSH de
l'administrateur » (`Either user must exist…`) : le compte d'administration
n'existe pas encore et `--check` ne le crée pas. C'est attendu ; les tâches qui
précèdent (garde-fous, paquets, création du compte) ont bien été simulées.

**8. Déploiement**

```bash
ansible-playbook site.yml -K
```

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
  Créer aussi `A` pour `www` : un joker `*` ne s'applique pas à un nom qui
  possède déjà un autre enregistrement (comme le TXT `www` que OVH ajoute par
  défaut), et `www` ne résolvait pas tant que son `A` n'existait pas. Chez
  Cloudflare, ces enregistrements étaient en « DNS only » (sans proxy) pendant
  les tests.
- `CAA 0 issue "letsencrypt.org"`.
- Freebox OS : bail DHCP statique, redirection **TCP 80 et 443 uniquement**.
  Le port 8443 n'est **pas** redirigé : il n'est joignable que depuis le LAN et le VPN.
- Jeton d'API DNS limité à la seule zone (Cloudflare : `Zone → DNS → Edit` et
  `Zone → Zone → Read`). Le plugin OVH n'existe pas dans Debian 13 : voir
  `certbot_dns_plugin` dans `group_vars/all/main.yml`.

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

### Application avec base de données (pod)

Une application qui déclare `sidecars:` devient un **pod** Podman : l'application
et ses conteneurs annexes (ici la base) partagent un réseau interne, et **seul le
port de l'application est publié**, sur `127.0.0.1`. La base n'est joignable de nulle
part ailleurs. L'exemple complet (Ghost + MySQL 8) est l'entrée `club` de
`public_hosts` dans `group_vars/all/main.yml`. Ghost n'est supporté qu'avec MySQL 8,
pas MariaDB.

**1. Secrets** : à ajouter dans le vault (`ansible-vault edit group_vars/all/vault.yml`).
Mots de passe aléatoires : `openssl rand -base64 32`. Le mot de passe de la boîte
d'envoi s'y saisit directement, sans passer par un terminal partagé.

```yaml
vault_app_secrets:
  club_db_password: "..."
  club_db_root_password: "..."
  club_smtp_password: "..."          # mot de passe de noreply@ (SMTP)
```

**2. Images** : `ghost` et `mysql` sont déjà épinglées par empreinte dans `images:`.
Pour les mettre à jour, changer le tag puis relancer `scripts/pin-digests.sh`.

**3. Sauvegarde** : copier un volume MySQL « à chaud » donne un fichier inutilisable.
On exporte donc la base avant chaque sauvegarde, et on exclut son volume :

```yaml
backup_pre_hooks:
  - >-
    cd / && runuser -u stack -- env XDG_RUNTIME_DIR=/run/user/$(id -u stack)
    podman exec app-pub-club-db sh -c
    'mysqldump --single-transaction -uroot -p"$(cat /run/secrets/club_db_root_password)" ghost'
    > {{ backup_dump_dir }}/club.sql
backup_excludes:
  - "*.tmp"
  - "*/volumes/club-db/*"
```

**Vérifié** : sur une VM Debian 13, MySQL démarre et s'initialise avec `ReadOnly=true` et les
capacités listées. Ghost, testé isolément dans les mêmes contraintes, démarre en root puis
bascule vers l'utilisateur `node` : sans capacité, cette bascule échoue (`failed switching to
'node'`). L'option `user: 1000` le lance directement en `node`. Le playbook contrôle en fin
de passage que chaque conteneur est actif : un conteneur qui plante en boucle ne fait pas
échouer `systemctl start`.

**Dépannage Ghost : `MigrationsAreLockedError`.** Si le pod ou la base est redémarré
pendant que Ghost crée ses tables (premier démarrage), le verrou de migration reste posé
et Ghost refuse de démarrer. Tant que le site est vide, recréer la base suffit ; **jamais
une fois qu'il contient du contenu** (restaurer alors `club.sql`, ou lever le verrou) :

```bash
(cd / && sudo -u stack XDG_RUNTIME_DIR=/run/user/$(id -u stack) podman exec app-pub-club-db sh -c 'mysql -uroot -p"$(cat /run/secrets/club_db_root_password)" -e "DROP DATABASE ghost; CREATE DATABASE ghost;"')
(cd / && sudo -u stack XDG_RUNTIME_DIR=/run/user/$(id -u stack) systemctl --user restart app-pub-club.service)
```

**Reste à vérifier avant la production** : les droits du volume `club-content`, et une
restauration réelle depuis `club.sql`.

## Page d'état

Une page en **lecture seule** compare ce que le playbook déclare à ce qui tourne
réellement : `https://etat.<domaine>:8443`, joignable depuis le LAN et le VPN
comme `whoami` (frontal privé : nftables puis HAProxy). Rien ne s'y commande :
la configuration reste dans Git et dans Ansible.

**Fonctionnement.** Une minuterie systemd (`selfhost-status.timer`, toutes les
5 minutes) lance `/usr/local/sbin/selfhost-status`, qui écrit `index.html` et
`status.json` dans `/srv/status/www`. Un conteneur nginx sans privilège (entrée
`etat` de `private_hosts`, image épinglée) les sert en lecture seule. Aucun
processus privilégié n'écoute sur le réseau. La liste attendue (services,
conteneurs, sites) est écrite par Ansible dans `/etc/selfhost-status/config.json` :
une unité qui disparaît est un **problème**, pas une ligne qui s'efface.

| Groupe | Vérifié |
|---|---|
| Sites | backend HAProxy `UP`, et réponse HTTP réelle (public : à travers HAProxy en TLS ; privé : directement sur l'application) ; 5xx = rouge, 4xx = orange |
| Applications | chaque conteneur et chaque conteneur annexe (base) de `stack` ; orange à partir de 5 redémarrages |
| Services du système | ssh, nftables, rsyslog, HAProxy, CrowdSec et son bouncer, certbot, mises à jour, minuteries de sauvegarde, smartd, mdmonitor |
| Certificat TLS | jours restants (orange < 21, rouge < 7), autorité (test ou production selon `acme_staging`), et **le certificat servi par HAProxy est bien celui du disque** (détecte un hook de renouvellement ou un rechargement en échec) |
| Sauvegardes | dernier instantané **lu dans chaque dépôt** (orange > 36 h, rouge > 72 h ; `status_backup_max_age_hours`), échec de la tâche, vérification hebdomadaire |
| Sécurité | politique `drop` de nftables, tables du bouncer, bouncer CrowdSec qui interroge le moteur, configuration **effective** de sshd (`sshd -T`), ports en écoute hors boucle locale non prévus (avec le processus) |
| Stockage | RAID dégradé ou en reconstruction, verdict SMART, espace disque et inodes (orange 80 %, rouge 90 %) |
| Hôte | charge, mémoire, horloge NTP, mises à jour en attente (dont sécurité), noyau plus récent installé, unités systemd en échec |

Quatre états : **OK**, **Info**, **Attention**, **Problème**, plus **Inconnu** :
un contrôle qui n'a pas pu répondre n'est jamais compté comme un succès. La page
affiche l'âge du relevé et passe un bandeau rouge si plus rien ne la met à jour
(15 minutes). Pour un site qui répond autrement que 2xx/3xx par conception,
déclarer dans son entrée de `public_hosts` / `private_hosts` :

```yaml
    probe_path: /ghost/api/admin/site/   # défaut : /
    probe_ok: [401]                      # codes HTTP en plus de 2xx/3xx
```

Commandes utiles :

```bash
sudo /usr/local/sbin/selfhost-status --text   # relevé immédiat, dans le terminal
sudo systemctl start selfhost-status          # relevé immédiat, dans la page
systemctl list-timers selfhost-status.timer
sudo journalctl -u selfhost-status -n 20
```

**Limites à connaître.**

- La page vit sur l'hôte : si l'hôte tombe, elle ne dit rien. `status.json`
  (champs `overall` et `generated_at`) est fait pour une sonde extérieure, par
  exemple la machine de sauvegarde qui l'interroge par WireGuard.
- Il n'y a pas d'alerte : il faut ouvrir la page.
- Les mesures lentes ou distantes sont mémorisées : sauvegardes 30 min, mises à
  jour et SMART 1 h.
- La page prouve que des instantanés **existent** dans le dépôt, pas qu'ils se
  **restaurent** : le test de restauration mensuel reste indispensable.
- L'écart avec Git n'est pas mesuré : `ansible-playbook site.yml --check --diff`.
- Sur une machine neuve, le rôle `status` se joue **avant** `apps` (le conteneur
  monte `/srv/status/www`) : ne pas lancer `--tags apps` seul avant la première
  passe complète. Le premier relevé a lieu 30 s après l'activation de la minuterie.

Tests du script (analyseurs, décisions, sonde HTTP/TLS contre de faux serveurs) :

```bash
python3 -m unittest discover -s tests -v
```

## Exploitation

La page d'état (section précédente) couvre l'essentiel au quotidien ; les
commandes ci-dessous servent à creuser.

```bash
# Applications
sudo -u stack XDG_RUNTIME_DIR=/run/user/$(id -u stack) systemctl --user status 'app-*'
(cd / && sudo -u stack XDG_RUNTIME_DIR=/run/user/$(id -u stack) podman ps)
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

**Déploiement réel sur une VM Debian 13.7** (ansible-core 2.19.11, Podman
5.4.2), rôle par rôle puis en une seule passe :

- `base` : compte d'administration, sshd durci (connexion par mot de passe
  refusée après rechargement), sudo, nftables, sysctl.
- `storage` : RAID et SMART ignorés sur une VM.
- `podman` et `apps` : utilisateur `stack` sans shell, image épinglée par
  empreinte, unités Quadlet démarrées, ports `127.0.0.1` non joignables depuis
  le réseau.
- `edge` : certificat wildcard par DNS-01 chez Cloudflare, d'abord de test puis
  de production ; HAProxy testé depuis un poste du LAN (200 sur `www`, 301
  apex → `www` et HTTP → HTTPS, frontal privé accessible depuis le réseau
  d'admin, hôte inconnu coupé) ; CrowdSec avec le dépôt `any/any`, bouncer
  nftables qui applique puis retire une décision, lecture des journaux HAProxy
  et SSH.
- `backup` : restic installé, « sauvegarde non configurée » affiché.
- Rejeu complet du playbook : `changed=0`.
- Redémarrage de la VM : SSH durci, nftables, HAProxy, CrowdSec et les
  applications reviennent seuls. Les applications démarrent environ 1 min 30
  après le système (Podman attend `network-online.target`) : HAProxy répond
  503 pendant ce délai.
- Test depuis la 4G, par les redirections 80 et 443 de la Freebox.

CrowdSec ignore les adresses privées : les requêtes du LAN ne déclenchent pas de
bannissement. Pour tester le blocage dans le noyau, utiliser une adresse de
documentation : `sudo cscli decisions add --ip 192.0.2.99 --duration 5m`, puis
`sudo nft list table ip crowdsec`, puis `sudo cscli decisions delete --ip 192.0.2.99`.

**Page d'état** : 34 tests (analyseurs sur des sorties réelles de systemctl,
HAProxy, mdstat, nft, `/proc/net/tcp` ; sonde HTTP, TLS et certificat servi contre
de faux serveurs ; une fuite d'identifiants restic et une injection HTML
cherchées sans succès), vérifiés aussi par mutation (casser un seuil ou une
comparaison fait échouer un test) ; rendu des gabarits Ansible ; page vue en
clair, en sombre et sur mobile. **Non vérifié sur la VM** : l'exécution du
service sous ses restrictions systemd (`ProtectSystem=full`, `NoNewPrivileges`)
avec `runuser`, `cscli` et `restic` ; nginx sans privilège avec `ReadOnly=true`
et un dossier hôte monté ; le format JSON de `cscli bouncers list` selon la
version de CrowdSec. Un contrôle qui échoue s'affiche « inconnu » avec sa cause.

**Non vérifié** : le renouvellement automatique du certificat (hook compris),
les sauvegardes restic et la cible `rest-server` (aucun dépôt configuré pendant
les essais), la surveillance RAID et SMART (une VM n'expose pas de SMART) et la
migration depuis Debian 12.

## Sauvegardes

Deux risques distincts, deux réponses distinctes :

- **Panne de disque** : RAID 1 (deux disques en miroir) et surveillance SMART,
  voir la section Stockage. Le miroir **n'est pas** une sauvegarde : une
  suppression, une corruption ou un rançongiciel sont recopiés sur les deux disques.
- **Perte de données** : sauvegardes restic, chiffrées et dédupliquées,
  selon la règle 3-2-1 (trois copies, deux supports, une hors site).

Le système n'est pas sauvegardé : le playbook le reconstruit. Seules les
**données** le sont : volumes Podman, certificats, exports de bases.

### Principe de la copie hors site : ajout seul

La machine qui reçoit les sauvegardes fait tourner rest-server en mode
`--append-only`. Le serveur sauvegardé peut **ajouter** des sauvegardes, jamais
en supprimer ni en modifier. Un attaquant qui prendrait le serveur ne pourrait
donc pas détruire l'historique. Conséquence : l'élagage des anciennes
sauvegardes se fait **à la main, depuis la machine cible**.

### Mise en place

**1. Secrets** : ajouter au vault (à coller seul, ouvre l'éditeur).

```bash
ansible-vault edit group_vars/all/vault.yml
```

Lignes à ajouter (générer les deux mots de passe avec `openssl rand -base64 32`) :

```yaml
vault_restic_password: "..."          # clé de chiffrement : la conserver AUSSI hors du serveur
vault_backup_rest_user: "selfhost"
vault_backup_rest_password: "..."
```

Sans `vault_restic_password`, **aucune restauration n'est possible**. Le garder
dans un gestionnaire de mots de passe ou sur papier, ailleurs que sur le serveur.

**2. Machine cible** (chez un proche, ou second serveur), joignable par le tunnel
WireGuard : la déclarer dans `inventory/hosts.yml`, groupe `backup_targets`, puis :

```bash
ansible-playbook backup-target.yml -K
```

**3. Dépôts** : dans `group_vars/all/main.yml`, renseigner `backup_repositories`
(exemple commenté dans `roles/backup/defaults/main.yml`), puis :

```bash
ansible-playbook site.yml -K --tags backup
```

### Contrôles

```bash
systemctl list-timers 'selfhost-backup*'
sudo journalctl -u selfhost-backup -n 30
sudo journalctl -u selfhost-backup-check -n 30
```

Lancer une sauvegarde immédiatement :

```bash
sudo systemctl start selfhost-backup
```

### Restaurer un fichier ou un dossier

Lister les sauvegardes du dépôt distant :

```bash
sudo env RESTIC_PASSWORD_FILE=/etc/selfhost-backup/password RESTIC_REPOSITORY_FILE=/etc/selfhost-backup/repos/distant restic snapshots
```

Restaurer la dernière sauvegarde dans un dossier à part, sans rien écraser :

```bash
sudo env RESTIC_PASSWORD_FILE=/etc/selfhost-backup/password RESTIC_REPOSITORY_FILE=/etc/selfhost-backup/repos/distant restic restore latest --target /root/restauration
sudo ls -R /root/restauration | head -50
```

### Test de restauration mensuel

Une sauvegarde jamais restaurée n'est qu'un espoir. Une fois par mois :

```bash
sudo env RESTIC_PASSWORD_FILE=/etc/selfhost-backup/password RESTIC_REPOSITORY_FILE=/etc/selfhost-backup/repos/distant restic restore latest --target /root/test-restauration
sudo du -sh /root/test-restauration
sudo rm -rf /root/test-restauration
```

### Reconstruction complète sur une nouvelle machine

1. Installer Debian 13 (RAID 1 au partitionnement si deux disques).
2. Cloner ce dépôt, restaurer `.vault_pass`, puis `ansible-playbook site.yml -K`.
3. Arrêter les applications :

```bash
sudo -u stack XDG_RUNTIME_DIR=/run/user/1500 systemctl --user stop 'app-*'
```

4. Restaurer les volumes à leur place :

```bash
sudo env RESTIC_PASSWORD_FILE=/etc/selfhost-backup/password RESTIC_REPOSITORY_FILE=/etc/selfhost-backup/repos/distant restic restore latest --target / --include /home/stack/.local/share/containers/storage/volumes
```

5. Redémarrer les applications :

```bash
sudo -u stack XDG_RUNTIME_DIR=/run/user/1500 systemctl --user start 'app-*'
```

L'utilisateur `stack` a un identifiant fixe (1500) : les fichiers restaurés
retrouvent leurs propriétaires. Les sous-identifiants des conteneurs
(`/etc/subuid`) dépendent de l'ordre de création des comptes ; si une
application ne lit plus ses fichiers, remettre les droits depuis le conteneur :

```bash
sudo -u stack XDG_RUNTIME_DIR=/run/user/1500 podman unshare chown -R 0:0 /home/stack/.local/share/containers/storage/volumes/<volume>/_data
```

### Élagage (sur la machine cible, à la main)

Le mot de passe de chiffrement est demandé : il n'est jamais stocké sur la cible.

```bash
sudo -u restic restic -r /srv/restic/selfhost forget --keep-daily 7 --keep-weekly 5 --keep-monthly 12 --prune
```

## Stockage

### RAID 1 : à la création, pas après

Le miroir se crée **à l'installation de Debian**, dans le partitionneur
(« RAID logiciel »). Le playbook ne touche jamais aux disques : il surveille,
et **s'arrête en erreur** s'il détecte un miroir dégradé.

```bash
cat /proc/mdstat                 # [UU] = sain, [U_] = un disque manquant
sudo mdadm --detail /dev/md0
```

### SMART

Sur une machine physique, smartd surveille chaque disque, lance un test court
chaque nuit et un test long le samedi. Les alertes vont au compte root, donc
dans le journal système ; un envoi par e-mail demande d'installer un relais de
messagerie (par exemple msmtp).

```bash
sudo smartctl -H /dev/sda        # verdict de santé
sudo smartctl -A /dev/sda        # attributs (secteurs réalloués, en attente...)
sudo journalctl -u smartd -n 30
```

### Remplacer un disque du miroir

Exemple : `sdb` est en panne, `sda` est sain, le miroir est `md0`.
Vérifier les noms avec `cat /proc/mdstat` **avant** chaque commande.

Retirer le disque défaillant du miroir :

```bash
sudo mdadm --manage /dev/md0 --fail /dev/sdb1
sudo mdadm --manage /dev/md0 --remove /dev/sdb1
```

Éteindre, remplacer physiquement le disque, redémarrer. Puis recopier la table
de partitions du disque sain vers le neuf, et donner au neuf ses propres
identifiants :

```bash
sudo sfdisk -d /dev/sda | sudo sfdisk /dev/sdb
sudo sgdisk -G /dev/sdb
```

Réintégrer le disque et suivre la reconstruction :

```bash
sudo mdadm --manage /dev/md0 --add /dev/sdb1
cat /proc/mdstat
```

Rendre le nouveau disque amorçable (démarrage BIOS) :

```bash
sudo grub-install /dev/sdb
```

En démarrage UEFI, la partition EFI n'est pas dans le miroir : la recopier
depuis le disque sain.

## Passage en production

1. Ajouter l'hôte physique dans `inventory/hosts.yml`, ajuster `admin_cidrs_v4`.
2. `acme_staging: false`, puis `ansible-playbook site.yml`.
3. Basculer la redirection de la Freebox vers la nouvelle IP.
4. Arrêter la VM.
