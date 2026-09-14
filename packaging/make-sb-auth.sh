#!/bin/bash
set -euo pipefail

OUT="."
CA_CERT=""
CA_KEY=""
DB_CERT=""
GUID="6f72656f-6e2d-5342-2d6b-657973000001"

usage() {
  echo "Run this on the signing PC that already has the Oreon CA private key."
  echo "It does not belong on the live ISO."
  echo
  echo "  $0 --ca-cert oreonsecurebootca.cer --ca-key oreonsecurebootca.key \\"
  echo "     --db-cert oreonsecureboot501.cer --out /usr/share/doc/kernel-keys/VERSION"
  echo
  echo "Puts PK.auth db.auth KEK.auth in --out. Rebuild the ISO after copying them there."
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --ca-cert) CA_CERT="${2:-}"; shift 2 ;;
    --ca-key) CA_KEY="${2:-}"; shift 2 ;;
    --db-cert) DB_CERT="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown arg $1"; usage ;;
  esac
done

[ -n "$CA_CERT" ] && [ -n "$CA_KEY" ] || usage
[ -f "$CA_CERT" ] || { echo "missing cert $CA_CERT"; exit 1; }
[ -f "$CA_KEY" ] || { echo "missing key $CA_KEY"; exit 1; }
command -v cert-to-efi-sig-list >/dev/null
command -v sign-efi-sig-list >/dev/null
command -v openssl >/dev/null
mkdir -p "$OUT"
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

to_pem() {
  if grep -q "BEGIN CERTIFICATE" "$1" 2>/dev/null; then
    cp "$1" "$2"
  else
    openssl x509 -inform DER -in "$1" -outform PEM -out "$2"
  fi
}

to_pem "$CA_CERT" "$WORKDIR/ca.pem"
if [ -n "$DB_CERT" ]; then
  [ -f "$DB_CERT" ] || { echo "missing db cert $DB_CERT"; exit 1; }
  to_pem "$DB_CERT" "$WORKDIR/db.pem"
else
  cp "$WORKDIR/ca.pem" "$WORKDIR/db.pem"
fi

cert-to-efi-sig-list -g "$GUID" "$WORKDIR/ca.pem" "$WORKDIR/PK.esl"
cert-to-efi-sig-list -g "$GUID" "$WORKDIR/ca.pem" "$WORKDIR/KEK.esl"
cert-to-efi-sig-list -g "$GUID" "$WORKDIR/db.pem" "$WORKDIR/db.esl"

sign-efi-sig-list -g "$GUID" -k "$CA_KEY" -c "$WORKDIR/ca.pem" PK "$WORKDIR/PK.esl" "$OUT/PK.auth"
sign-efi-sig-list -g "$GUID" -k "$CA_KEY" -c "$WORKDIR/ca.pem" KEK "$WORKDIR/KEK.esl" "$OUT/KEK.auth"
sign-efi-sig-list -g "$GUID" -k "$CA_KEY" -c "$WORKDIR/ca.pem" db "$WORKDIR/db.esl" "$OUT/db.auth"

echo "wrote $OUT/PK.auth $OUT/db.auth $OUT/KEK.auth"
