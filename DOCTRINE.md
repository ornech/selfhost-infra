# Doctrine

Chaque mesure de ce dépôt répond à une leçon documentée, principalement issue de
l'expérience ukrainienne depuis 2022. Le critère de choix d'un outil n'est pas
sa nationalité, mais sa conformité à ces leçons, sa maturité et la possibilité
de le comprendre et de l'auditer.

## 1. Centraliser crée des points de défaillance uniques

**Leçon.** Avant l'invasion, la loi ukrainienne imposait l'hébergement des
données publiques sur le territoire. Une semaine avant le 24 février 2022, le
parlement l'a modifiée pour permettre leur migration ; les données de dizaines
d'administrations ont été évacuées vers des clouds hors du pays. Microsoft
résume : la capacité à déplacer rapidement les données hors du pays tout en
continuant à les exploiter est la clé de la résilience numérique en temps de guerre.

**Dans ce dépôt.**
- Le playbook est **rejouable** : l'hôte n'est pas une pièce unique, il se
  reconstruit sur une autre machine (VM, serveur, VPS) en une commande.
- Aucune configuration faite à la main sur l'hôte : tout est dans Git.
- Sauvegarde restic chiffrée, règle 3-2-1, avec une copie **hors site en
  ajout seul** : le serveur peut écrire, jamais effacer. Prendre le serveur ne
  suffit pas à détruire l'historique (un rançongiciel ne peut pas l'atteindre).
- Vérification d'intégrité hebdomadaire et procédure de test de restauration :
  une sauvegarde jamais restaurée n'est qu'un espoir.
- Miroir RAID 1 et surveillance SMART contre la panne matérielle, en sachant
  qu'un miroir recopie aussi les dégâts : il ne remplace pas la sauvegarde.

## 2. Micro-segmenter

**Leçon.** Après l'attaque destructrice de décembre 2023, le PDG de Kyivstar a
expliqué que l'opérateur avait déjà une segmentation, mais visait désormais la
micro-segmentation. CERT-UA recommande la micro-segmentation pour limiter la
propagation latérale.

**Dans ce dépôt.**
- Les applications n'écoutent que sur `127.0.0.1` : seul HAProxy les atteint.
- Deux frontaux distincts : public (80/443) et privé (8443, réseaux d'admin
  uniquement, filtré deux fois : nftables puis HAProxy).
- Applications sous un utilisateur non privilégié (Podman rootless) :
  une évasion de conteneur ne donne pas root.
- *Phase serveur physique* : DMZ sur VLAN dédié derrière OPNsense.

## 3. La chaîne d'approvisionnement est une surface d'attaque

**Leçon.** L'attaque contre Viasat a détourné des mécanismes légitimes de mise
à jour, et CERT-UA a documenté plusieurs compromissions de la chaîne
d'approvisionnement dans les attaques contre le secteur énergétique en 2024.

**Dans ce dépôt.**
- Le périmètre exposé (HAProxy, certbot, rsyslog, nftables) provient des
  **paquets Debian signés**, corrigés par l'équipe de sécurité Debian et
  installés automatiquement par unattended-upgrades.
- Un seul dépôt tiers, signé : CrowdSec.
- Aucun binaire compilé à la volée, aucun plugin téléchargé depuis GitHub.
- Images de conteneurs **épinglées par empreinte sha256** ; le playbook refuse
  un tag seul. Une mise à jour d'image est un acte volontaire, tracé dans Git.
- La CI refuse un vault en clair et cherche des secrets dans l'historique (gitleaks).

## 4. L'humain est le premier vecteur d'entrée

**Leçon.** Dans les grandes attaques analysées, l'accès initial passe presque
toujours par une personne : compte compromis, hameçonnage.

**Dans ce dépôt.**
- SSH par clé uniquement, pas de mot de passe, root interdit, joignable
  seulement depuis le LAN et le VPN : deux facteurs de possession
  indépendants (clé WireGuard, clé SSH protégée par passphrase).
- Aucune interface d'administration exposée à Internet.
- Secrets dans Ansible Vault, jamais en clair dans Git ni dans les fichiers de
  configuration des applications (secrets Podman).

## 5. Détecter vite et partager

**Leçon.** La défense contre Industroyer2 (2022) a réussi parce que CERT-UA,
ESET et Microsoft se sont coordonnés à partir des enseignements de 2016. Le
ministère ukrainien recommande de se raccorder aux plateformes d'échange MISP.

**Dans ce dépôt.**
- CrowdSec analyse les journaux HAProxy et SSH, et bloque **au niveau du
  noyau** (nftables) avant même qu'HAProxy ne voie la connexion.
- Il bénéficie du renseignement communautaire (listes d'IP signalées par
  l'ensemble des instances).
- *À explorer* : règles Sigma (SOC Prime, équipe en Ukraine, en lien avec
  CERT-UA) et flux MISP pour une couche de détection supplémentaire.

## 6. Réduire la surface

**Dans ce dépôt.**
- Seuls 80 et 443 sont redirigés par la Freebox.
- Un sous-domaine inconnu reçoit une coupure de connexion, sans page d'erreur
  ni bannière (`Server` supprimé).
- Certificat wildcard : les noms des services n'apparaissent pas un par un
  dans les journaux Certificate Transparency.
- Pas d'AAAA tant que l'IPv6 n'est pas filtré finement.
- Pas de SSO tiers devant les services privés : ils ne sont pas exposés du tout.

## Sources

- Microsoft, *Defending Ukraine: Early Lessons from the Cyber War* (juin 2022),
  cité par Military Times : https://www.militarytimes.com/2022/06/22/how-the-cloud-saved-ukraines-data-from-russian-attacks
- AWS, *Safeguarding Ukraine's data* : https://go.aws/3E7q8CK
- The Record, entretien avec le PDG de Kyivstar : https://therecord.media/kyivstar-ceo-on-russian-cyberattack-telecom
- Tech Trenches, *The First Full-Scale Cyber War: 4 Years of Lessons* : https://techtrenches.dev/p/the-first-full-scale-cyber-war-4
- Intecracy, recommandations CERT-UA : https://intecracy.com/?p=15622
- Ministère ukrainien du Développement, recommandations face aux cyberattaques destructrices :
  https://mindev.gov.ua/storage/app/sites/1/uploaded-files/rekomendaciyi-shhodo-minimizaciyi-rizikiv-destruktivnogo-kibervplivu.pdf
- SOC Prime, Sigma2SaveLives : https://socprime.com/news/sigma2savelives-equip-yourself-with-curated-sigma-rules-while-donating-to-save-lives-in-ukraine/
- Podman, préservation de l'IP source en rootless : https://github.com/eriksjolund/podman-networking-docs
  et https://github.com/podman-container-tools/podman/pull/28478
- CrowdSec, collection HAProxy (type `syslog` pour un fichier écrit par syslog) :
  https://app.crowdsec.net/hub/author/crowdsecurity/collections/haproxy
