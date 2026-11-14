#!/bin/sh
# Résout chaque image "registre/nom:tag" de group_vars/all/main.yml en
# "registre/nom:tag@sha256:<empreinte de l'index multi-architecture>".
#
# L'empreinte est calculée localement sur le manifeste brut téléchargé :
# c'est exactement ce que Podman vérifiera au moment du pull.
#
# Usage : scripts/pin-digests.sh            (affiche le bloc images: à recopier)
# Dépendances : skopeo, python3-yaml
set -eu

vars_file="$(dirname "$0")/../group_vars/all/main.yml"

python3 - "$vars_file" <<'PY' | while read -r key ref; do
import sys, yaml
images = yaml.safe_load(open(sys.argv[1]))["images"]
for k, v in images.items():
    print(k, v.split("@", 1)[0])   # repart du tag, ignore une ancienne empreinte
PY
  tmp="$(mktemp)"
  # Manifeste brut écrit dans un fichier : un échec réseau ne doit JAMAIS
  # produire l'empreinte d'un contenu vide.
  if ! skopeo inspect --raw "docker://$ref" > "$tmp" || [ ! -s "$tmp" ]; then
    rm -f "$tmp"; echo "Échec : impossible de lire le manifeste de $ref" >&2; exit 1
  fi
  digest="sha256:$(sha256sum "$tmp" | cut -d' ' -f1)"
  rm -f "$tmp"
  printf '  %s: "%s@%s"\n' "$key" "$ref" "$digest"
done
