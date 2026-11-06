# secrets/

Docker Compose mounts these files into the container at `/run/secrets/`.
Git ignores everything here except this file.

| File | What it holds | How to make it |
|------|---------------|----------------|
| `pocketid_client_secret` | The PocketID OIDC client secret | Copy from PocketID |
| `jwt_signing_key` | Signs the gateway's own OAuth tokens | `scripts/make-secrets.sh` |
| `storage_encryption_key` | Fernet key that encrypts the OAuth client store | `scripts/make-secrets.sh` |

The container runs as UID 10001. Compose mounts file secrets with their host
owner and mode, so give the files to that user:

```sh
sudo chown 10001:10001 secrets/pocketid_client_secret secrets/jwt_signing_key secrets/storage_encryption_key
sudo chmod 400 secrets/pocketid_client_secret secrets/jwt_signing_key secrets/storage_encryption_key
```
