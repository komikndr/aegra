# ONYX staging deployment

These playbooks update the existing staging stack on `10.10.5.11`.
They use the existing `stack.sh staging` commands and VM-local configuration.
The controller requires Ansible Core, Git, and both source repositories.
The VM requires Python 3, Docker Compose V2, and Docker access for `kms`.

## Existing configuration

The default VM layout is:

```text
/home/kms/aegra/
/home/kms/executive-ai-canvas/
/home/kms/onyx_ai_secret/
  env.staging.backend
  env.staging.frontend
  keycloak-realm-aegra.staging.json
```

The playbooks do not copy, print, or replace these secret files.
Source archives contain tracked files from exact local release commits.
Remote Git credentials and published Git tags are not required.
The existing repositories remain available after deployment.

## SSH access

1. Check the VM fingerprint from your existing VM terminal:

   ```sh
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

2. Connect from the Ansible controller:

   ```sh
   ssh kms@10.10.5.11
   ```

3. Compare the fingerprint before accepting the host key.

Ansible uses normal SSH host verification.
The commands below request the SSH password interactively with `--ask-pass`.
SSH key authentication also works without that flag.

## Read-only preflight

1. Copy `inventory.example.yml` to a private inventory outside Git.
2. Set the repository and secret directory paths to the VM's actual paths.
3. Run the inspection from this directory:

   ```sh
   ansible-playbook -i /path/to/inventory.yml preflight.yml --ask-pass
   ```

The report includes Compose project names, running services, image IDs, and named volume identities.
It shows public service origins without URL credentials or query strings.
It does not print environment files, realm contents, or container environment values.
Preflight does not start, stop, or recreate containers.

Resolve the reported blockers before deployment.
Check that public service origins use `10.10.5.11`, rather than old `10.10.5.190` defaults.
The playbooks do not edit authentication URLs or Keycloak clients.
If the secret directory is under `gpu_platform`, change `onyx_secret_dir` in the inventory.

An application SQLite database must use persistent storage before Aegra recreation.
The playbook refuses an unmounted analytic SQLite database.
Existing application data mounts retain their original bind paths or external volume names.
Source and build mounts that hide the release image block deployment.
An external primary database requires a separate backup procedure before this playbook can proceed.

## Deploy the release

1. Complete preflight and check the source release tags locally.
2. Run:

   ```sh
   ansible-playbook -i /path/to/inventory.yml deploy.yml --ask-pass \
     -e onyx_deploy=true \
     -e onyx_release=v1.6.0 \
     -e onyx_frontend_source=/home/kxn/vega_ai/executive-ai-canvas
   ```

`onyx_backend_source` defaults to this backend repository.
Set it explicitly when the controller uses another repository layout.
This update-only playbook requires existing running infrastructure.

The deployment follows this sequence:

1. Run preflight again.
2. Resolve both local release tags to commit IDs.
3. Transfer Git archives into separate release directories on the VM.
4. Build both application images before replacing either application.
5. Save a PostgreSQL custom-format dump with private file permissions.
6. Save the previous image IDs, project names, and named volume identities.
7. Update only `aegra`, preserving the existing backend Compose project name.
8. Wait for backend HTTP health.
9. Update only `app`, preserving the existing frontend Compose project name.
10. Wait for frontend HTTP readiness.

The backend staging command applies database migrations during startup.
The playbook does not run `down`, remove volumes, or recreate PostgreSQL, Keycloak, Redis, or MinIO.
It does not run the Keycloak or MinIO initialization jobs.
The PostgreSQL dump does not back up Keycloak, MinIO, or external data sources.
Those services retain their current containers and volumes.

Application images retain the existing Compose project image names.
Saved image IDs identify previous images after a new build updates those names.
Build and application replacement failures stop deployment.
The playbook does not automatically reverse database migrations.
An operator must review schema compatibility before a rollback or database restore.

After deployment, update inventory repository paths to the reported release directories.
For manual Compose operations in those directories, include the generated mount override:

```sh
COMPOSE_PROJECT_NAME=<existing-project-name> \
STAGING_ENV_FILE=/home/kms/onyx_ai_secret/env.staging.backend \
sh ./stack.sh staging -f .onyx-runtime.json ps
```

Then run preflight again and check login, chat streaming, charts, and PDF export in a browser.
HTTP readiness alone does not verify those authenticated workflows.

## Local validation

```sh
ansible-playbook -i inventory.example.yml preflight.yml --syntax-check
ansible-playbook -i inventory.example.yml deploy.yml --syntax-check
```

These syntax checks do not connect to the VM.
An actual VM preflight is still required before deployment.
