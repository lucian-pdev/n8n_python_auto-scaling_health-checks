## Testing Documentation

Run the whole stack (containers + systemd timers + firewall) on a throwaway Debian or Ubuntu machine, with test
settings: random passwords, self-signed certificate, ports open on the network so you can click around.
Your live config (`../env`) is never used.

### 1. Pick a machine

Any fresh Debian 12/13 or Ubuntu 22.04/24.04 machine (VM, LXC, spare server) with a sudo user.
Docker is installed by `deploy.sh` if missing. Use a trusted network: test ports are reachable by anyone on it.

### 2. Deploy

Pick one of these.

**A. Directly on the test machine** (you're logged into it, repo cloned there):

```bash
./deploy.sh testing_files/env
```

**B. From your own computer to a machine by IP** (repo checked out on your computer, on the branch you want to test):

```bash
TARGET=user@192.0.2.10    # the test machine
DEST=naypyers             # folder on it (relative to the user's home)

git ls-files -co --exclude-standard | COPYFILE_DISABLE=1 tar czf - --no-xattrs -T - \
    | ssh "$TARGET" "mkdir -p $DEST && tar xzf - -C $DEST"
ssh -t "$TARGET" "cd $DEST && ./deploy.sh testing_files/env"
```

Run the same two commands again after a code change: the code is updated, the test passwords are kept.
(`COPYFILE_DISABLE=1 ... --no-xattrs` stops macOS tar from adding `._*` metadata files; harmless on Linux.)

Always pass `testing_files/env`: plain `./deploy.sh` uses the live template, which binds every port to the
machine's localhost, so nothing but 80/443 is reachable from your browser.

**C. With a staging tool** that reads `testing_files/stage.conf` (creates the VM, copies the repo, deploys,
waits until python-api answers):

```bash
<staging-tool> /path/to/naypyers
```

### 3. Find your logins

On the test machine:

```bash
grep -E "^(DOMAIN|GF_SECURITY_ADMIN_USER|GF_SECURITY_ADMIN_PASSWORD)=" ~/naypyers/.env
```

`DOMAIN` is a random name like `n8n-1a2b3c4d.localhost`. Tell your computer where it lives (once per test machine):

```bash
echo "<test-machine-IP> <DOMAIN>" | sudo tee -a /etc/hosts
```

### 4. Open the apps

Your browser warns about the certificate once (it's self-signed): accept it.

| App | Open | Log in with |
|-----|------|-------------|
| n8n | `https://<DOMAIN>/` | First visit: create the owner account |
| Grafana | `https://<DOMAIN>/grafana/` or `http://<IP>:3000/grafana/` | `GF_SECURITY_ADMIN_USER` / `GF_SECURITY_ADMIN_PASSWORD` |
| Prometheus | `http://<IP>:9090` | - |
| python-api health | `http://<IP>:8000/health` | - |
| Traefik dashboard | `http://<IP>:8082/dashboard/` | - |

n8n refuses to log you in over plain `http://<IP>:5678` (secure cookies). On a test machine the simplest fix is to
allow it before deploying: `sed -i "s/^N8N_SECURE_COOKIE=.*/N8N_SECURE_COOKIE=False/" .env`.
Otherwise use the `https://<DOMAIN>/` address, or a tunnel: `ssh -L 5678:localhost:5678 user@<IP>`, then `http://localhost:5678`.

### 5. Optional: sync scripts from your own repo

python-api runs scripts that the `github_sync` timer pulls from a git repo (by default the public one in the template).
To test against your own repo (GitHub, Gitea/Forgejo, GitLab), on the test machine:

```bash
cd ~/naypyers
sed -i "s|^SCRIPTS_REPO=.*|SCRIPTS_REPO='https://git.example.com/owner/repo.git'|" .env
sed -i "s|^GITHUB_TOKEN=.*|GITHUB_TOKEN='<read-only token>'|" .env    # private repos only
sudo systemctl start github_sync.service && journalctl -u github_sync.service -n 20
```

With a staging tool, export `SCRIPTS_REPO`, `SCRIPTS_BRANCH`, `SCRIPTS_TOKEN` before running it instead.

### 6. Start over or remove

On the test machine, in `~/naypyers`:

```bash
sudo systemctl disable --now autoscaler.timer github_sync.timer
docker compose down -v
sudo rm -rf n8n-data job-output scripts
rm .env                                   # next deploy creates new passwords
```

Or simply delete the VM. Remove its line from your `/etc/hosts`.

### Upgrade rehearsal

`repro.conf` and `repro/` rehearse `upgrade/` on a fresh VM, for staging tools that read them (Project_Testing
`repro.sh`, Jenkins `repro-upgrade` with `OS_UPDATE=true` and an old-kernel template):

| Hook | Script | Does |
|------|--------|------|
| deploy-old | `repro/procedure.sh deploy-old` | this version with n8n pinned to `REPRO_N8N_OLD` |
| seed | `repro/seed.sh` | n8n owner + saved login, encrypted credential, scheduled workflow writing heartbeats, production webhook |
| baseline / verify | `repro/check-app.sh` | heartbeats keep coming (credential decrypts), webhook direct + through Traefik, login still valid |
| fetch / os-before / os-after | `repro/procedure.sh` | upgrade scripts of the new ref, backup, patient stop, OS update, reboot, everything back |
| upgrade / verify | `repro/procedure.sh` | backup, switch-code, migrate-env, n8n -> `REPRO_N8N_NEW`, preflight, apply, verify |

The Hostinger upgrade (nginx -> Traefik, NB database) has its own procedure and rehearsal on the `hostinger` branch.

### For maintainers: changing the test config

`env` here mirrors `../env` (live) key for key; only values differ (staging certificate, open ports, debug logs,
1-minute sync, max 3 workers). After adding a setting to either, check they still match:

```bash
diff <(grep -oE '^[A-Z_]+=' env | sort) <(grep -oE '^[A-Z_]+=' testing_files/env | sort)
```

Other files here: `stage.conf` (instructions for staging tools), `test_data.json` (sample webhook payload),
`standardise_phone.py` (sample script).
