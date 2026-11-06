#!/bin/sh
# Create the secrets/ files. Run from the repository root, once.
set -eu
umask 077
mkdir -p secrets
[ -s secrets/jwt_signing_key ] || openssl rand -base64 48 > secrets/jwt_signing_key
# A Fernet key is 32 random bytes, URL-safe base64.
[ -s secrets/storage_encryption_key ] || openssl rand -base64 32 | tr '+/' '-_' > secrets/storage_encryption_key
[ -e secrets/pocketid_client_secret ] || : > secrets/pocketid_client_secret
chmod 600 secrets/jwt_signing_key secrets/storage_encryption_key secrets/pocketid_client_secret
echo "Created secrets/. Put the PocketID client secret in secrets/pocketid_client_secret, then run:"
echo "  sudo chown 10001:10001 secrets/jwt_signing_key secrets/storage_encryption_key secrets/pocketid_client_secret"
echo "  sudo chmod 400 secrets/jwt_signing_key secrets/storage_encryption_key secrets/pocketid_client_secret"
