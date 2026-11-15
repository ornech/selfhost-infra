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

## Les comptes

Trois comptes système, et aucun n'est destiné aux utilisateurs ordinaires : les
membres de l'association ont leurs comptes dans les applications (Ghost, Gancio),
jamais sur le serveur.

| Compte | Rôle | UID | Connexion |
|---|---|---|---|
| `root` | Secours, **au clavier de la machine seulement** | 0 | Console. Jamais en SSH. Mot de passe testé |
| `sysadmin` | Administration (`admin_user`) | 1000 | SSH par clé, `sudo` sans mot de passe ; mot de passe verrouillé à l'étape 10 |
| `stack` | Applications (Podman sans privilèges) | 1500, fixe | Aucune : pas de shell, mot de passe verrouillé |

- **Les personnes sont des clés, pas des comptes.** `admin_ssh_pubkeys` est une
  liste : chaque administrateur y met sa propre clé publique, et les journaux SSH
  enregistrent l'empreinte de la clé qui s'est connectée.
- **`sysadmin` est le compte créé à l'installation de Debian** : il porte ce nom dès
  le départ, le playbook le configure sans en créer un autre. Un autre nom reste
  possible (`admin_user`), sauf `root`, que le playbook refuse.
- **`root` n'est jamais modifié par le playbook.** C'est la porte de secours : si SSH
  est mal réglé, on entre à l'écran de la machine.
- Le compte avec lequel on lance le playbook n'est écrit nulle part dans la
  configuration de sshd.

**Installer Debian 13 pour ce dépôt** :

- **Mot de passe root** : à définir, puis **à tester tout de suite** sur une seconde
  console (`Alt+F2`). Un mot de passe tapé à l'aveugle peut différer du clavier
  d'une session à l'autre.
- **Utilisateur ordinaire** : nom complet « Administrateur système », identifiant
  `sysadmin`.
- **Logiciels** : « serveur SSH » et « utilitaires usuels du système », sans
  environnement graphique.
- **Disques** : partitionnement manuel et RAID 1 si la machine a deux disques
  (section Stockage).

## Mise en route

Chaque bloc se colle **seul**, dans l'ordre. Aucun éditeur, aucun script :
chaque ligne dit ce qu'elle modifie, et une commande de contrôle suit.

**0. Outils et dépôt.** Une seule fois, en root : donner `sudo` à `sysadmin`. Ce
premier bloc se tape **seul** (la ligne suivante serait lue comme le mot de passe) :

```bash
su -
```

Puis, dans le shell root, ce bloc, qui se termine en ouvrant une session `sysadmin` :

```bash
apt install -y sudo
usermod -aG sudo sysadmin
su - sysadmin
```

Ensuite, **sous `sysadmin`** pour toute la suite :

```bash
sudo apt install -y ansible skopeo git openssl
[ -d ~/selfhost-infra ] || git clone https://github.com/ornech/selfhost-infra.git ~/selfhost-infra
cd ~/selfhost-infra && git pull
```

**1. Mot de passe du vault** (aléatoire, ignoré par Git, à sauvegarder hors du serveur)

```bash
if [ -e .vault_pass ]; then
  echo "DÉJÀ PRÉSENT : .vault_pass n'est pas écrasé (un nouveau mot de passe rendrait un vault existant illisible)"
else
  openssl rand -base64 32 > .vault_pass
  chmod 600 .vault_pass
fi
ls -l .vault_pass
```

Le contrôle doit afficher un fichier de 45 octets, en `-rw-------`.

**2. Tes valeurs** (à renseigner avant de coller : les lignes sont volontairement vides)

```bash
DOMAINE=
EMAIL=
CLE=
```

- `DOMAINE` : ton nom de domaine, celui de ta zone Cloudflare, sans `www`.
- `EMAIL` : une adresse qui reçoit vraiment les courriers de Let's Encrypt.
- `CLE` : la clé **publique** SSH de ton **poste** (pas du serveur), sur une seule
  ligne et entre apostrophes. Sur ton poste, `cat ~/.ssh/id_ed25519.pub` l'affiche ;
  elle n'est pas secrète. Si tu n'en as pas, crée-la d'abord, avec une phrase de
  passe : `ssh-keygen -t ed25519 -C "$(whoami)@$(hostname)"`. Laisse `CLE=` vide
  seulement si tu es connecté à cette machine par clé SSH : elle est alors lue dans
  `~/.ssh/authorized_keys`.

Coller le bloc sans l'avoir rempli donne un ÉCHEC à l'étape suivante, pas un
déploiement avec des valeurs d'exemple.

**3. Report dans `group_vars/all/main.yml`**

```bash
if [ -n "$DOMAINE" ] && [ -n "$EMAIL" ]; then
  sed -i "s|^domain: .*|domain: $DOMAINE|" group_vars/all/main.yml
  sed -i "s|^acme_email: .*|acme_email: $EMAIL|" group_vars/all/main.yml
else
  echo "ÉCHEC : DOMAINE ou EMAIL est vide : refaire l'étape 2"
fi
[ -n "$CLE" ] || CLE=$(grep -m1 -E '^(ssh|ecdsa|sk)-' ~/.ssh/authorized_keys 2>/dev/null)
if printf '%s\n' "$CLE" | ssh-keygen -lf - >/dev/null 2>&1; then
  sed -i '/^admin_ssh_pubkeys:/{n;s|.*|  - "'"$CLE"'"|}' group_vars/all/main.yml
  printf '%s\n' "$CLE" | ssh-keygen -lf -
else
  echo "ÉCHEC : aucune clé publique complète : la renseigner dans CLE (étape 2)"
fi
grep -nE '^(domain|acme_email|admin_user):|^  - "(ssh|ecdsa|sk)-' group_vars/all/main.yml
```

Le contrôle affiche d'abord l'**empreinte** de la clé (`SHA256:…`) : elle doit être
identique à celle de ton poste (`ssh-keygen -lf ~/.ssh/id_ed25519.pub`). Puis
**quatre lignes** : `domain`, `acme_email`, `admin_user` (qui vaut `sysadmin`) et la
clé. S'il en manque une, ou si un ÉCHEC est apparu, ne pas continuer. La ligne
d'exemple `AAAA…` n'est pas une clé : elle est refusée.

**4. Deux secrets à saisir** : sans affichage et sans trace dans l'historique. Le jeton
d'API Cloudflare, puis le mot de passe de la boîte `noreply@` de ton domaine (le site
du club, publié par la configuration d'exemple, s'en sert pour envoyer ses courriels).
**Un bloc à la fois** : un second `read` collé avec le premier lirait la ligne suivante
comme valeur.

```bash
read -rsp "Jeton Cloudflare : " CF_TOKEN; echo
```

Colle le jeton à l'invite, puis Entrée (rien ne s'affiche). Le dernier mot,
`CF_TOKEN`, est le **nom** de la variable, pas la valeur. Puis :

```bash
read -rsp "Mot de passe de la boîte noreply@ (SMTP) : " SMTP; echo
```

Contrôle que les deux longueurs ne sont pas nulles (53 pour un jeton `cfut_…`) :

```bash
echo "jeton : ${#CF_TOKEN} caractères ; mot de passe SMTP : ${#SMTP} caractères"
```

**5. Création et chiffrement du vault.** Le fichier est chiffré **au moment où il est
écrit** : le jeton et les mots de passe ne sont jamais en clair sur le disque. Les deux
mots de passe de la base du club sont générés ici et ne s'affichent pas. **Cette étape
ne se rejoue pas** : si `vault.yml` existe, elle refuse de l'écraser (il contient des
secrets déjà utilisés par les applications).

```bash
Q="'"
if [ ! -s .vault_pass ]; then
  echo "ÉCHEC : .vault_pass absent ou vide : refaire l'étape 1, le vault n'est pas créé"
elif [ -e group_vars/all/vault.yml ]; then
  echo "ÉCHEC : group_vars/all/vault.yml existe déjà : il n'est pas écrasé (il contient des secrets déjà utilisés)"
elif [ -z "$CF_TOKEN" ] || [ -z "$SMTP" ]; then
  echo "ÉCHEC : jeton ou mot de passe SMTP vide, le vault n'est pas créé"
else
  {
    echo '---'
    echo 'vault_certbot_dns_credentials: |'
    printf '  dns_cloudflare_api_token = %s\n' "$CF_TOKEN"
    echo 'vault_app_secrets:'
    printf '  club_db_password: "%s"\n' "$(openssl rand -hex 24)"
    printf '  club_db_root_password: "%s"\n' "$(openssl rand -hex 24)"
    printf "  club_smtp_password: '%s'\n" "${SMTP//$Q/$Q$Q}"
  } | ansible-vault encrypt --output group_vars/all/vault.yml - >/dev/null \
    && chmod 600 group_vars/all/vault.yml \
    || echo "ÉCHEC : le vault n'est pas créé"
fi
unset CF_TOKEN SMTP Q
head -1 group_vars/all/vault.yml
```

La dernière ligne doit afficher `$ANSIBLE_VAULT;1.1;AES256`. Si elle affiche autre
chose, ou si un ÉCHEC est apparu, le vault n'existe pas : ne pas continuer. Pour
vérifier **les noms** des secrets sans jamais voir leurs valeurs :

```bash
ansible-vault view group_vars/all/vault.yml | grep -E '^(vault_app_secrets|  [a-z_]+):' | cut -d: -f1
```

Il doit lister `vault_app_secrets`, puis `club_db_password`, `club_db_root_password`
et `club_smtp_password`.

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

À la fin du rôle `base`, un rappel indique que SSH n'est **pas encore verrouillé** :
c'est normal, c'est l'objet des deux étapes suivantes.

**9. Tester la connexion SSH par clé.** Depuis ton **poste** (un nouveau terminal),
sans fermer la session de la machine :

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 sysadmin@IP_DE_LA_MACHINE 'whoami; sudo -n true && echo sudo OK'
```

Attendu : `sysadmin`, puis `sudo OK`. `IdentitiesOnly` est nécessaire : sshd coupe
après trois refus (`MaxAuthTries 3`), et un agent SSH qui propose plusieurs clés les
épuise avant la bonne (`Too many authentication failures`). Pour ne plus y penser,
mettre dans `~/.ssh/config` du poste un bloc `Host` avec `IdentityFile` et
`IdentitiesOnly yes`. **Ne passe pas à l'étape 10 tant que ce test n'a pas réussi.**

**10. Verrouiller SSH.** Seulement après l'étape 9 :

```bash
sed -i 's|^ssh_lockdown: .*|ssh_lockdown: true|' group_vars/all/main.yml
grep -n '^ssh_lockdown:' group_vars/all/main.yml
ansible-playbook site.yml -K --tags base
```

Le contrôle doit afficher `ssh_lockdown: true`. Le playbook interdit alors les mots
de passe, n'autorise plus que `admin_user`, et verrouille son mot de passe. Depuis le
poste, la connexion par mot de passe doit être refusée, et celle par clé (étape 9)
doit marcher toujours :

```bash
ssh -o PubkeyAuthentication=no sysadmin@IP_DE_LA_MACHINE
```

Attendu : `Permission denied (publickey)`. Tant que l'étape 10 n'est pas faite, la
ligne « Durcissement SSH » de la page d'état est rouge : c'est voulu.

### Ne pas se couper l'accès

Le verrouillage de SSH se fait en deux temps (étapes 9 et 10), et c'est ce qui
empêche une clé fausse de vous enfermer dehors : rien n'est verrouillé avant que la
connexion par clé ait été testée. Les garde-fous du rôle `base` refusent aussi, avant
toute modification, une clé qui n'est pas une clé publique complète. En plus :

- vérifier que le réseau depuis lequel tu te connectes est bien dans
  `admin_cidrs_v4` (`ip -4 addr` sur ton poste) ;
- garder une session ouverte pendant les passes, et en tester une **nouvelle** avant
  de fermer l'ancienne ;
- si SSH est mal réglé malgré tout, le secours est l'écran de la machine : connexion
  en `root` avec le mot de passe testé à l'installation.

`acme_staging: true` par défaut : le certificat vient de l'autorité de **test**
de Let's Encrypt (avertissement navigateur, pas de limite de débit). Une fois le
déploiement validé, passer à `false` et rejouer : le certificat est réémis en production.

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

**1. Secrets** : les trois secrets du club (`club_db_password`, `club_db_root_password`,
`club_smtp_password`) sont **créés à l'étape 5** de la mise en route. Pour une autre
application qui déclare `secrets:`, ajouter ses noms sous la clé **existante**
`vault_app_secrets`, avec `ansible-vault edit group_vars/all/vault.yml` (mots de passe
aléatoires : `openssl rand -hex 24`) :

```yaml
vault_app_secrets:
  club_db_password: "..."            # déjà là
  club_db_root_password: "..."       # déjà là
  club_smtp_password: "..."          # déjà là : mot de passe de noreply@ (SMTP)
  app_db_password: "..."             # nouveau
```

Ne jamais écrire une **seconde** clé `vault_app_secrets` : avec deux clés identiques,
Ansible garde la dernière en émettant seulement un avertissement
(`Found duplicate mapping key`), et les secrets de la première sont ignorés sans autre
signe. Le playbook **refuse de démarrer** si une application réclame un secret absent de
`vault_app_secrets` : le message liste les noms manquants. Pour ne pas publier le club,
retirer l'entrée `club` de `public_hosts` dans `group_vars/all/main.yml`.

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

**Reste à vérifier avant la production** : les droits du volume `club-content`, et le
remplacement de la vraie base par `club.sql` (l'export se recharge sans erreur dans une base
temporaire, voir « Restaurer la base du club »).

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
# Sauvegardes (détail : section Sauvegardes)
systemctl list-timers 'selfhost-backup*' --no-pager
systemctl show selfhost-backup -p Result            # success : la dernière a réussi
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
- Verrouillage SSH en deux temps : le fichier sshd rendu dans les deux états, lu par
  `sshd -T` (mots de passe acceptés et aucune liste de comptes tant que
  `ssh_lockdown` vaut `false` ; clé seule et `AllowUsers` ensuite) ; les garde-fous du
  rôle `base` joués sur 14 jeux de valeurs ; les blocs des étapes 0 à 10 vérifiés
  dans des copies jetables.

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
  - Hook de déploiement de certbot, lancé de force par
    `certbot renew --dry-run --run-deploy-hooks` : le renouvellement simulé réussit
    avec le jeton Cloudflare, le fichier `/etc/haproxy/certs/<domaine>.pem` est
    réassemblé (sa date est celle du test) et HAProxy se recharge dans la même
    seconde, puis reste `active`.
- `backup` (dépôt local `/srv/backup/restic`, puis dépôt distant sur `rest-server`) :
  - Déploiement, simulation `--check` sans erreur, rejeu à `changed=0`.
  - Export MySQL lancé par systemd sous `NoNewPrivileges` ; `club.sql` produit ; le volume
    `club-db` est bien exclu (0 fichier sauvegardé) et `club-content` sauvegardé.
  - Minuterie de nuit déclenchée seule (03:22) ; sauvegardes incrémentales.
  - Vérification hebdomadaire (`restic check`, 5 %) : `intègre` ; mauvais mot de passe refusé.
  - Restauration dans un dossier à part : `club.sql`, `/etc/letsencrypt` et `club-content`
    identiques (liens symboliques compris), mêmes propriétaires. `club.sql` rechargé dans une
    base temporaire : 105 tables et mêmes comptes que la base réelle.
  - Deux défauts trouvés et corrigés : la vérification échouait après 28 minutes sur un dépôt
    local (`ProtectSystem=strict` rendait le dépôt non inscriptible), et restic n'avait aucun
    cache sous systemd (`RESTIC_CACHE_DIR`).
- `backup_target` (sur la même VM, en boucle locale) : le chemin du binaire était faux sur
  Debian 13 (`/usr/bin/restic-rest-server`) ; déploiement puis rejeu à `changed=0` ; mode
  ajout seul (suppression refusée par `403`, accès sans identifiants refusé par `401`) ;
  port 8000 fermé depuis le réseau local.
- Cible éteinte : la sauvegarde reprend seule si elle revient pendant l'attente ; sinon
  échec après 14 min 39 s, puis sauvegarde locale normale et tâche en erreur.
- Rejeu complet du playbook : `changed=0`.
- Redémarrage de la VM : SSH durci, nftables, HAProxy, CrowdSec et les
  applications reviennent seuls. Les applications démarrent environ 1 min 30
  après le système (Podman attend `network-online.target`) : HAProxy répond
  503 pendant ce délai.
- Test depuis la 4G, par les redirections 80 et 443 de la Freebox.

**Déploiement réel sur une machine physique x86** (Debian 13, Freebox directe), en
suivant ce README :

- Playbook complet à `failed=0`, puis rejeu complet sous `sysadmin` à `changed=0` ;
  `club` et `www` répondent `200` depuis le LAN avec le certificat de test.
- SSH : connexion par clé et `sudo` sans mot de passe sous `sysadmin` ; après le
  verrouillage (étape 10), un mot de passe est refusé (`Permission denied (publickey)`)
  et la clé marche toujours.
- Les essais ont fait corriger six défauts : `admin_user` valant `root`, clé SSH vide,
  valeurs d'exemple collées telles quelles, vault laissé en clair, secrets du club
  absents du vault, et un compte `ansible_user` hérité de l'ancienne VM dans
  `AllowUsers`. Chacun est désormais refusé par un garde-fou ou évité par le README.
- **Redirection de la Freebox vers cette machine avec le certificat de test :
  Brave affiche `ERR_CERT_AUTHORITY_INVALID` et, à cause de HSTS, sans possibilité
  de passer outre.** Ne rediriger les ports 80 et 443 vers une machine qu'avec un
  certificat de production et le contenu du site déjà restauré.

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

**Non vérifié** : un vrai renouvellement lancé par la minuterie de certbot (le
renouvellement simulé et le hook réussissent, mais le hook n'a tourné que sur le
certificat déjà en place, pas sur un certificat fraîchement émis), le parcours des
comptes de la section « Les comptes » depuis une installation **neuve** où
l'installateur Debian crée `sysadmin` (il a été vérifié sur la machine physique, mais
après réparation à la main de ses comptes), le certificat de **production** et la
restauration du club sur la machine physique, la reconstruction complète sur une
machine neuve et le remplacement de la vraie base du club par un export,
la règle de pare-feu `backup_target_client_cidrs` (elle demande deux machines),
l'élagage depuis la cible, la surveillance RAID et SMART (la machine physique est
installée, mais ses sorties `/proc/mdstat` et `smartctl` n'ont pas encore été relevées)
et la migration depuis Debian 12.

## Sauvegardes

Deux risques distincts, deux réponses distinctes :

- **Panne de disque** : RAID 1 (deux disques en miroir) et surveillance SMART,
  voir la section Stockage. Le miroir **n'est pas** une sauvegarde : une
  suppression, une corruption ou un rançongiciel sont recopiés sur les deux disques.
- **Perte de données** : sauvegardes restic, chiffrées et dédupliquées,
  selon la règle 3-2-1 (trois copies, deux supports, une hors site).

Le système n'est pas sauvegardé : le playbook le reconstruit. Seules les
**données** le sont : volumes Podman, certificats, exports de bases.

### Comment ça marche

Chaque nuit, une minuterie systemd lance le script `selfhost-backup`, qui fait trois choses :

1. **Exporter les bases.** Une base de données copiée « à chaud » est inutilisable. Les
   commandes de `backup_pre_hooks` en écrivent donc un export cohérent (pour le club :
   `/var/backups/selfhost/club.sql`).
2. **Copier les données** dans chaque dépôt déclaré (`backup_repositories`) avec restic.
   restic découpe les fichiers en morceaux, ne stocke que ce qui a changé depuis la
   dernière fois (seule la première sauvegarde est complète) et **chiffre** tout avec la
   clé `vault_restic_password`.
3. **Noter le résultat** dans le journal : `-> <dépôt> : OK` ou `ÉCHEC`. Un dépôt en échec
   n'empêche pas les autres d'être sauvegardés ; la tâche se termine alors en erreur.

Chaque passage crée un **instantané** (snapshot) : l'état des fichiers à cet instant. On peut
restaurer n'importe lequel. Une fois par semaine, un second script (`selfhost-backup-check`)
vérifie que les dépôts ne sont pas corrompus, en relisant 5 % des données.

### Où tout est configuré

| Quoi | Où | Détail |
|---|---|---|
| Où envoyer les sauvegardes | `backup_repositories`, dans `group_vars/all/main.yml` | Liste de dépôts, chacun avec un nom et une adresse. Une adresse qui commence par `/` est un **dossier de cette machine** ; `rest:http://…` désigne un serveur distant |
| Ce qui est sauvegardé | `backup_paths` (valeur par défaut dans `roles/backup/defaults/main.yml`) | Volumes Podman de `stack`, `/etc/letsencrypt`, `/var/backups/selfhost` |
| Ce qui est exclu | `backup_excludes` | Par exemple le volume MySQL du club, remplacé par son export |
| Exports avant la copie | `backup_pre_hooks` | Commandes lancées en root avant chaque sauvegarde |
| Heures | `backup_schedule`, `backup_check_schedule` | Sauvegarde à 03:15 plus 0 à 15 minutes au hasard ; vérification le dimanche à 05:00 |
| Clé de chiffrement | `vault_restic_password`, dans le vault | Écrite sur le serveur dans `/etc/selfhost-backup/password` |

Ce que le playbook installe sur le serveur sauvegardé :

```
/etc/selfhost-backup/password        clé de chiffrement (0600, root)
/etc/selfhost-backup/repos/<nom>     adresse de chaque dépôt (0600 : elle peut contenir un mot de passe)
/etc/selfhost-backup/excludes        exclusions
/usr/local/sbin/selfhost-backup      script de sauvegarde
/usr/local/sbin/selfhost-backup-check   script de vérification
/etc/systemd/system/selfhost-backup{,-check}.{service,timer}
/var/backups/selfhost/               exports de bases (0700)
/root/.cache/restic/                 cache de restic
```

**Le dossier de sauvegarde lui-même** est l'adresse du dépôt : par exemple
`/srv/backup/restic` pour un dépôt local, ou `/srv/restic/<compte>/` sur la machine
cible (`backup_target_data_dir`). Ce n'est pas un dossier de copies lisibles : restic y
range des morceaux chiffrés (`data/`, `index/`, `snapshots/`…). On ne le consulte qu'avec la
commande `restic`, voir plus bas.

Un dépôt placé sur le **même disque** que les données ne protège pas d'une panne de disque :
il sert à revenir en arrière après une erreur. La copie qui compte est celle de la machine
distante.

### Principe de la copie hors site : ajout seul

La machine qui reçoit les sauvegardes fait tourner rest-server en mode
`--append-only`. Le serveur sauvegardé peut **ajouter** des sauvegardes, jamais
en supprimer ni en modifier. Un attaquant qui prendrait le serveur ne pourrait
donc pas détruire l'historique. Conséquence : l'élagage des anciennes
sauvegardes se fait **à la main, depuis la machine cible**.

Vérifié : depuis le serveur sauvegardé, `restic forget` reçoit `403 Forbidden` et le nombre
d'instantanés ne baisse pas ; un accès sans identifiants reçoit `401`.

### Mise en place

**1. Secrets** : ajouter au vault (à coller seul, ouvre l'éditeur).

```bash
ansible-vault edit group_vars/all/vault.yml
```

Lignes à ajouter. La clé restic se génère avec `openssl rand -base64 32`. Le mot de passe du
compte rest-server se génère avec `openssl rand -hex 24` : il est inséré dans l'adresse du
dépôt (`rest:http://compte:mot-de-passe@…`), où un `/` du format base64 casserait l'adresse.

```yaml
vault_restic_password: "..."          # clé de chiffrement : la conserver AUSSI hors du serveur
vault_backup_rest_user: "selfhost"
vault_backup_rest_password: "..."
```

Sans `vault_restic_password`, **aucune restauration n'est possible**. Le garder
dans un gestionnaire de mots de passe ou sur papier, ailleurs que sur le serveur
(voir « Ce qu'il faut garder hors du serveur »).

**2. Machine cible** (chez un proche, ou second serveur), joignable par le tunnel
WireGuard : la déclarer dans `inventory/hosts.yml`, groupe `backup_targets`, puis :

```bash
ansible-playbook backup-target.yml -K
```

À lancer **vers la machine cible uniquement**. Ce playbook applique aussi le socle (`base`)
avec les ports 80 et 443 fermés : sur le serveur principal, il couperait l'accès au site.
Pour l'essayer sans seconde machine, limiter aux tâches du rôle et viser la machine
elle-même par un inventaire temporaire (c'est ainsi qu'il a été testé) :

```bash
cat > ~/inv-cible.yml <<'FIN'
all:
  children:
    backup_targets:
      hosts:
        vm-cible:
          ansible_connection: local
FIN
ansible-playbook -i ~/inv-cible.yml backup-target.yml -K --tags backup_target
```

Sur Debian 13, le paquet `restic-rest-server` installe `/usr/bin/restic-rest-server`
(`backup_target_bin`). Le pare-feu de la cible n'ouvre le port 8000 qu'aux adresses de
`backup_target_client_cidrs` (vide par défaut : personne). Vérifié avec la liste vide : le
port est fermé depuis le réseau local. L'ouverture à une adresse listée n'a pas été
essayée : elle demande deux machines.

**3. Dépôts** : dans `group_vars/all/main.yml`, renseigner `backup_repositories`
(exemple commenté dans `roles/backup/defaults/main.yml`), puis :

```bash
ansible-playbook site.yml -K --tags backup
```

**Piège** : `group_vars/all/` charge **tous** ses fichiers par ordre alphabétique, et le
dernier qui définit une variable l'emporte, sans message. Ne pas mettre `backup_repositories`
(ni aucune variable non secrète) dans le vault. Pour voir ce qu'Ansible retient réellement
(les noms seulement, jamais les adresses qui contiennent un mot de passe) :

```bash
ansible <hôte> -m debug -a 'msg={{ backup_repositories | map(attribute="name") | list }}'
```

### Vérifier l'état des sauvegardes

Un raccourci pour la session : restic avec la clé du serveur, sur le dépôt de votre choix
(`local`, `distant`… : le nom donné dans `backup_repositories`) :

```bash
rr() { sudo env RESTIC_PASSWORD_FILE=/etc/selfhost-backup/password RESTIC_REPOSITORY_FILE=/etc/selfhost-backup/repos/"$1" restic "${@:2}"; }
```

Les contrôles :

```bash
systemctl list-timers 'selfhost-backup*' --no-pager      # prochaine exécution, dernière exécution
systemctl show selfhost-backup -p Result,ExecMainStatus  # Result=success : la dernière a réussi
sudo journalctl -u selfhost-backup -n 30 --no-pager      # détail : une ligne « -> <dépôt> : OK » par dépôt
rr local snapshots                                       # liste des instantanés (date de chacun)
sudo du -sh /srv/backup/restic                           # place prise par un dépôt local
sudo journalctl -u selfhost-backup-check -n 30 --no-pager   # vérification hebdomadaire
```

Comment lire :

- Tout va bien : `Result=success`, une ligne `OK` par dépôt, un instantané de moins de 24 h.
- `ÉCHEC` : lire les lignes `Fatal:` juste au-dessus dans le journal (cible injoignable,
  mauvais mot de passe, disque plein).
- Aucun instantané récent : la minuterie n'a pas tourné (machine éteinte, unité arrêtée).
- Vérification hebdomadaire : `-> <dépôt> : intègre`, ou `PROBLÈME`.

Lancer une sauvegarde immédiatement (attend la fin) :

```bash
sudo systemctl start selfhost-backup
```

### Quand la cible est hors service

Si la machine distante ne répond pas (panne, tunnel coupé), restic réessaie plusieurs
minutes avant d'abandonner : **14 min 39 s mesurées**. Les dépôts sont traités dans l'ordre
alphabétique de leur nom ; un dépôt distant nommé avant un dépôt local retarde donc d'autant
la sauvegarde locale, qui est faite ensuite normalement. La tâche finit en erreur
(`Result=exit-code`). Si la cible revient pendant l'attente, la sauvegarde reprend seule
(mesuré : 7 min 24 s d'attente, puis `OK`).

### Restaurer un fichier ou un dossier

Restaurer toujours **dans un dossier à part**, regarder, puis copier ce qu'il faut. Lister
les instantanés, puis restaurer le dernier (ou un identifiant précis) :

```bash
rr distant snapshots
rr distant restore latest --target /root/restauration
sudo ls -R /root/restauration | head -50
```

Pour ne restaurer qu'une partie : `--include /chemin/absolu`. **Supprimer ensuite le
dossier** (`sudo rm -rf /root/restauration`) : il contient les clés privées des certificats
et la base du club, en clair.

### Restaurer la base du club

L'export `club.sql` est dans `/var/backups/selfhost/` (et dans chaque instantané). Un
raccourci pour lancer `mysql` dans le conteneur de la base :

```bash
my() { (cd / && sudo runuser -u stack -- env XDG_RUNTIME_DIR=/run/user/$(id -u stack) podman exec -i app-pub-club-db sh -c 'mysql -uroot -p"$(cat /run/secrets/club_db_root_password)" "$@"' sh "$@"); }
```

**Vérifier qu'un export est lisible, sans toucher au site** : le charger dans une base
temporaire, comparer, la supprimer (vérifié : 105 tables, mêmes comptes que la base réelle).

```bash
sudo grep -cE 'GTID_PURGED|CREATE DATABASE|^USE ' /var/backups/selfhost/club.sql   # attendu : 0
sudo -v
my -e "CREATE DATABASE ghost_test"
sudo cat /var/backups/selfhost/club.sql | my ghost_test
my -e "SELECT table_schema, COUNT(*) FROM information_schema.tables WHERE table_schema IN ('ghost','ghost_test') GROUP BY table_schema"
my -e "DROP DATABASE ghost_test"
```

**Remplacer la vraie base** par l'export (perte de tout ce qui a été écrit depuis l'export).
Arrêter Ghost sans arrêter la base, recréer la base vide, charger, redémarrer :

```bash
(cd / && sudo -u stack XDG_RUNTIME_DIR=/run/user/$(id -u stack) systemctl --user stop app-pub-club.service)
my -e "DROP DATABASE ghost; CREATE DATABASE ghost"
sudo cat /var/backups/selfhost/club.sql | my ghost
(cd / && sudo -u stack XDG_RUNTIME_DIR=/run/user/$(id -u stack) systemctl --user start app-pub-club.service)
```

Cette séquence n'a pas encore été rejouée sur la vraie base ; chacune de ses commandes
l'a été séparément, sauf `DROP DATABASE ghost` suivi du chargement.

### Test de restauration mensuel

Une sauvegarde jamais restaurée n'est qu'un espoir. Une fois par mois :

```bash
rr distant restore latest --target /root/test-restauration
sudo du -sh /root/test-restauration
sudo rm -rf /root/test-restauration
```

### Reconstruction complète sur une nouvelle machine

Cas : le serveur est perdu, il faut tout remonter ailleurs. Ce qu'il faut avoir **hors du
serveur** est listé plus bas ; sans cela, les sauvegardes ne peuvent pas être lues.

1. Installer Debian 13 (RAID 1 au partitionnement si deux disques), puis suivre « Mise en
   route » : cloner ce dépôt, remettre `.vault_pass`, `group_vars/all/vault.yml`,
   `group_vars/all/main.yml` et `inventory/hosts.yml` (les copies gardées hors du serveur).
   Les dépôts et la clé restic doivent être **les mêmes** qu'avant : c'est ce qui permet de
   relire les anciennes sauvegardes.
2. `ansible-playbook site.yml -K` : le système est reconstruit, les applications démarrent
   **vides**. Si le dépôt existe et que la clé est bonne, le playbook ne l'initialise pas
   (vérifié au rejeu).
3. Arrêter la minuterie, pour qu'aucune sauvegarde du site vide ne devienne « le dernier
   instantané » :

   ```bash
   sudo systemctl stop selfhost-backup.timer
   ```

4. Repérer l'instantané voulu (colonne `Host` = l'ancienne machine), noter son identifiant :

   ```bash
   rr distant snapshots
   ```

5. Arrêter les applications concernées, puis restaurer les volumes et les exports :

   ```bash
   sudo -u stack XDG_RUNTIME_DIR=/run/user/1500 systemctl --user stop 'app-*'
   rr distant restore <identifiant> --target / --include /home/stack/.local/share/containers/storage/volumes --include /var/backups/selfhost
   ```

   Le volume de la base du club (`club-db`) revient **vide** : il est exclu de la sauvegarde
   au profit de l'export `club.sql`.
6. Redémarrer les applications, puis recharger la base depuis `/var/backups/selfhost/club.sql`
   (section « Restaurer la base du club ») :

   ```bash
   sudo -u stack XDG_RUNTIME_DIR=/run/user/1500 systemctl --user start 'app-*'
   ```

7. Contrôler le site, puis relancer la minuterie : `sudo systemctl start selfhost-backup.timer`.

L'utilisateur `stack` a un identifiant fixe (1500) : les fichiers restaurés
retrouvent leurs propriétaires (vérifié : même propriétaire avant et après pour le volume
du club). Les sous-identifiants des conteneurs (`/etc/subuid`) dépendent de l'ordre de
création des comptes ; si une application ne lit plus ses fichiers, remettre les droits
depuis le conteneur :

```bash
sudo -u stack XDG_RUNTIME_DIR=/run/user/1500 podman unshare chown -R 0:0 /home/stack/.local/share/containers/storage/volumes/<volume>/_data
```

**Non rejoué de bout en bout** : les étapes ont été vérifiées séparément (restauration dans un
dossier, rechargement de l'export dans une base temporaire), pas la séquence complète sur une
machine neuve.

### Ce qu'il faut garder hors du serveur

Si le serveur disparaît, il emporte avec lui tout ce qui n'est pas dans Git. À conserver
ailleurs, **dans au moins deux endroits dont un hors de chez vous** :

1. `.vault_pass` : sans lui le vault est illisible.
2. Une copie de `group_vars/all/vault.yml` (chiffré) : il contient la clé restic, l'adresse
   et le compte de la machine distante, les secrets des applications.
3. La clé restic **seule**, à part : sans elle, les sauvegardes sont des données chiffrées
   sans recours possible.
4. Une copie de `group_vars/all/main.yml` et de `inventory/hosts.yml` tels qu'ils sont sur
   le serveur : le domaine, le courriel et la clé SSH y sont des modifications locales,
   absentes de Git.

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
