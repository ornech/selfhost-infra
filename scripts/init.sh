#!/bin/sh
# Initialisation sans éditeur : pose les questions, renseigne
# group_vars/all/main.yml, crée et chiffre group_vars/all/vault.yml,
# puis épingle les images par empreinte.
#
# Usage (depuis n'importe où) : scripts/init.sh
set -eu
cd "$(dirname "$0")/.."

main=group_vars/all/main.yml
vault=group_vars/all/vault.yml

ask() {  # ask <invite> <défaut> -> stdout
  printf '%s [%s] : ' "$1" "$2" >&2
  read -r rep
  printf '%s' "${rep:-$2}"
}
ask_secret() {  # saisie masquée
  printf '%s : ' "$1" >&2
  stty -echo 2>/dev/null || true; read -r rep; stty echo 2>/dev/null || true; printf '\n' >&2
  printf '%s' "$rep"
}

# --- Mot de passe du vault -------------------------------------------------
if [ ! -s .vault_pass ]; then
  umask 077
  openssl rand -base64 32 > .vault_pass
  echo "-> .vault_pass généré (aléatoire). Sauvegarde-le hors de ce serveur."
fi

# --- Variables non secrètes ------------------------------------------------
domain=$(ask "Nom de domaine" "")
[ -n "$domain" ] || { echo "Domaine obligatoire." >&2; exit 1; }
email=$(ask "E-mail Let's Encrypt" "admin@$domain")
admin=$(ask "Compte d'administration à créer" "jf")
pubfile=$(ask "Fichier de clé publique SSH de l'admin" "$HOME/.ssh/authorized_keys")
[ -s "$pubfile" ] || { echo "Fichier introuvable ou vide : $pubfile" >&2; exit 1; }
plugin=$(ask "Plugin DNS certbot (ovh, cloudflare)" "ovh")

python3 - "$main" "$domain" "$email" "$admin" "$pubfile" "$plugin" <<'PY'
import sys, re
path, domain, email, admin, pubfile, plugin = sys.argv[1:]
keys = [l.strip() for l in open(pubfile) if l.strip().startswith(("ssh-", "ecdsa-", "sk-"))]
if not keys:
    sys.exit(f"Aucune clé publique SSH dans {pubfile}")
s = open(path).read()
def sub(pattern, repl):
    global s
    s, n = re.subn(pattern, repl, s, count=1, flags=re.M)
    if n != 1:
        sys.exit(f"Motif introuvable dans {path} : {pattern}")
sub(r'^domain: .*$', f'domain: {domain}')
sub(r'^acme_email: .*$', f'acme_email: {email}')
sub(r'^admin_user: .*$', f'admin_user: {admin}')
sub(r'^certbot_dns_plugin: .*$', f'certbot_dns_plugin: {plugin}')
sub(r'^admin_ssh_pubkeys:\n(?:  - .*\n)+',
    'admin_ssh_pubkeys:\n' + ''.join(f'  - "{k}"\n' for k in keys))
open(path, 'w').write(s)
print(f"-> {path} renseigné ({len(keys)} clé(s) SSH)")
PY

# --- Secrets (vault) -------------------------------------------------------
if [ -f "$vault" ] && head -n1 "$vault" | grep -q '^\$ANSIBLE_VAULT;'; then
  echo "-> $vault existe déjà (chiffré) : inchangé. Modifier avec : ansible-vault edit $vault"
else
  case "$plugin" in
    ovh)
      echo "Clé d'API OVH limitée à la zone (https://eu.api.ovh.com/createToken/)"
      ak=$(ask "  application_key" "")
      as=$(ask_secret "  application_secret")
      ck=$(ask_secret "  consumer_key")
      creds="dns_ovh_endpoint = ovh-eu
dns_ovh_application_key = $ak
dns_ovh_application_secret = $as
dns_ovh_consumer_key = $ck" ;;
    cloudflare)
      tok=$(ask_secret "Token Cloudflare (Zone:DNS:Edit sur la seule zone)")
      creds="dns_cloudflare_api_token = $tok" ;;
    *)
      echo "Plugin $plugin : identifiants à saisir ensuite avec ansible-vault edit $vault"
      creds="# identifiants du plugin $plugin" ;;
  esac
  umask 077
  {
    echo "---"
    echo "vault_certbot_dns_credentials: |"
    printf '%s\n' "$creds" | sed 's/^/  /'
    echo "vault_app_secrets: {}"
  } > "$vault"
  ansible-vault encrypt "$vault" >/dev/null
  echo "-> $vault créé et chiffré"
fi

# --- Images par empreinte --------------------------------------------------
echo "-> Épinglage des images (skopeo)..."
if pins=$(scripts/pin-digests.sh); then
  PINS="$pins" python3 - "$main" <<'PY'
import os, sys, re
path = sys.argv[1]
s = open(path).read()
for line in os.environ["PINS"].splitlines():
    m = re.match(r'\s+(\w+): "(.+)"', line)
    if m:
        key, ref = m.groups()
        s, n = re.subn(rf'^(  {key}: )"[^"]*"', rf'\g<1>"{ref}"', s, count=1, flags=re.M)
        print(f"   {key} -> {ref}" if n else f"   {key} : introuvable dans {path}")
open(path, 'w').write(s)
PY
else
  echo "   Échec (réseau ?). Relancer plus tard scripts/init.sh : les étapes déjà faites sont conservées."
fi

echo
echo "Terminé. Vérifier puis déployer :"
echo "  git diff $main"
echo "  ansible-playbook site.yml --check --diff -K"
