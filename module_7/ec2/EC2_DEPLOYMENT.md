# Deploying the Module 6 stack to EC2

This is how the Module 6 system (Flask web, worker, PostgreSQL, RabbitMQ) runs on one EC2 instance
with Docker Compose. Nothing is built on the server: it pulls the published images
`frametotoro/module_6:web-v1` and `frametotoro/module_6:worker-v1` from Docker Hub, plus `postgres:16`
and `rabbitmq:3.13-management`.

Placeholders: `<EC2_PUBLIC_IPV4>` (the instance's public address), `<KEY_NAME>` (the key pair's name)
and `<WINDOWS_USER>` (the Windows user folder). The deployment record below and the EC2 results in
section 9 are what I actually ran and saw.

## Deployment record

| Item | Value |
| --- | --- |
| Date | 2026-10-18 |
| Region | `us-west-2` (US West, Oregon), the same Region as the bucket |
| AMI | Ubuntu Server 24.04 LTS (HVM), SSD Volume Type, 64-bit (x86), `ami-096f5760b00bcd95c`; the instance reports `Ubuntu 24.04.4 LTS`, kernel `6.17.0-1017-aws` |
| Instance type | `t3.micro` (2 vCPUs, 911 MiB of memory as `free -m` reports it), `us-west-2a` |
| Instance ID | `i-0728e111a4728989d`, name `gradcafe-module7` |
| Public IPv4 used | `54.187.45.90` (auto-assigned; it changes after every stop and start) |
| Security group | `sg-062ad54f2147a4aba` (`launch-wizard-1`, the name the launch form gives it), inbound 22 and 8080 from my IP only, one `/32` address (masked in `ec2-security-group.png` because the repository is public) |
| Key pair | `gradcafe-ms` (RSA, `.pem`), kept at `~/.ssh/gradcafe-ms.pem` in WSL with mode 400 |
| Root volume | 16 GiB gp3 (`df -h /`: 15G, 4.7G used after the pull and the load) |
| Swap file added | yes, 1 GiB (step 5). With the stack running: 640 of 911 MiB of memory used and 124 MiB in swap, so it was needed |
| Anything that differed from the steps below | (1) The launch form's Quick Start list no longer offers plain Ubuntu 22.04 (only "Ubuntu Server 22.04 LTS with SQL Server 2022 Standard", which adds a SQL Server licence charge), so the AMI is 24.04. (2) Changing the AMI in the launch form reset the security group rules to SSH only; the 8080 rule was added afterwards with **Edit inbound rules** (step 2). (3) The worker's first connection to RabbitMQ failed (`RabbitMQ is not reachable yet (attempt 1 of 10 ...)`) and the retry 3 s later connected. |

```text
$ docker --version
Docker version 29.1.3, build 29.1.3-0ubuntu3~24.04.2
$ docker compose version
Docker Compose version 2.40.3+ds1-0ubuntu1~24.04.1
```

## What runs where

```text
 laptop (WSL)                                     EC2 instance, folder ~/gradcafe, Docker Compose
 ~/gradcafe-ec2/ ------ scp, 3 files --------->   docker-compose.ec2.yml, .env, data/applicant_data.json
 browser ------ http://<EC2_PUBLIC_IPV4>:8080 ->  web       port 8080; security group: my IP only
 ssh -L 15672:127.0.0.1:15672 ---------------->   rabbitmq  UI on the instance's 127.0.0.1:15672 only
                                                  db        not published (5432 stays inside)
                                                  init      runs once, then exits 0
                                                  worker    takes the tasks off the queue
```

## 1. Launch the instance

EC2 console, **Launch instance**:

| Setting | Value |
| --- | --- |
| Name | `gradcafe-module7` |
| AMI | **Ubuntu Server 24.04 LTS**, 64-bit (x86). Ubuntu 22.04 LTS works with the same commands. |
| Instance type | `t3.micro` (1 GiB of memory, see section 5). Not a `t4g` type: the images are built for x86 (linux/amd64) only. |
| Key pair | **Create new key pair**, name `<KEY_NAME>`, format `.pem`. The browser saves it in `Downloads`. |
| Network settings | **Create security group**; **Allow SSH traffic from: My IP**. Auto-assign public IP: Enable (the default VPC does this). |
| Storage | **16 GiB, gp3**. The default 8 GiB also fits, but with little room to spare: Ubuntu itself takes roughly 2 GB, Docker about 0.4 GB, the four images 1.6 GB (measured: postgres:16 642 MB, rabbitmq:3.13-management 392 MB, worker 290 MB, web 243 MB), the swap file from step 5 1 GiB, and the database about 70 MB. gp3 costs $0.08 per GB-month, so 16 GiB is about $1.28 a month while the volume exists. Check with `df -h /` after `pull` (step 8). |
| IAM instance profile | none. The stack does not call AWS. |

After **Launch instance**, open the instance and wait until **Instance state** is **Running** and the
status checks have passed. Screenshot of the details page with the state and the **Public IPv4 address**:
`ec2-instance.png`.

## 2. Security group: 22 and 8080 from my IP only

Instance > **Security** tab > the security group > **Edit inbound rules** > **Add rule**:

| Type | Port | Source |
| --- | --- | --- |
| SSH | 22 | My IP (one address, `/32`) |
| Custom TCP | 8080 | My IP (one address, `/32`) |

No other inbound rules: no 5432 (PostgreSQL), no 15672 (RabbitMQ UI), no `0.0.0.0/0`. Outbound stays
at the default (all traffic), which `apt-get`, `docker pull` and the worker's Grad Café scrape need.
Screenshot of the inbound rules: `ec2-security-group.png`.

WSL2 goes out through Windows' network connection, so "My IP" in the browser is also the address SSH
comes from. If that address changes (another network, a VPN), SSH and the page time out until the two
rules are edited to the new **My IP**.

## 3. SSH in from WSL

`ssh` refuses a key that other users can read, and every file under `/mnt/c` looks world-readable from
WSL. So copy the key to the Linux side first:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
cp /mnt/c/Users/<WINDOWS_USER>/Downloads/<KEY_NAME>.pem ~/.ssh/
chmod 400 ~/.ssh/<KEY_NAME>.pem
ssh -i ~/.ssh/<KEY_NAME>.pem ubuntu@<EC2_PUBLIC_IPV4>
```

The first connection asks `Are you sure you want to continue connecting (yes/no/[fingerprint])?`:
answer `yes`. The user name is `ubuntu` on Ubuntu (it is `ec2-user` on Amazon Linux).

## 4. Install Docker and the Compose plugin

On the instance (Ubuntu 24.04 or 22.04):

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2
sudo systemctl enable --now docker          # start now and at every boot
sudo usermod -aG docker $USER               # run docker without sudo ...
exit                                        # ... after logging in again
```

```bash
ssh -i ~/.ssh/<KEY_NAME>.pem ubuntu@<EC2_PUBLIC_IPV4>
docker --version
docker compose version
docker run --rm hello-world
```

`newgrp docker` also applies the new group, but only in the shell it starts; logging in again applies
it everywhere.

The assignment's example installs `docker-compose-plugin`. That package exists only in Docker's own apt
repository, not in Ubuntu's: on stock Ubuntu 22.04 and 24.04 the command stops with
`E: Unable to locate package docker-compose-plugin`. Ubuntu's own package for the same `docker compose`
plugin is `docker-compose-v2`. Tested in October 2026 in the stock `ubuntu:22.04` and `ubuntu:24.04`
images: `docker.io` 29.1.3 and `docker-compose-v2` 2.40.3 install from the Ubuntu archive, and
`docker compose config` accepts `docker-compose.ec2.yml` with that version.

**Amazon Linux 2023 instead of Ubuntu** (user `ec2-user`). Its repositories have Docker but no Compose
plugin, so the plugin comes from the Compose release page:

```bash
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -fsSL https://github.com/docker/compose/releases/download/v2.40.3/docker-compose-linux-x86_64 \
     -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
exit    # log in again, then: docker --version; docker compose version
```

(Tested in the `amazonlinux:2023` image: Docker 25.0.14 and Compose v2.40.3; `config` accepts the file.)

## 5. Add a swap file (t3.micro)

A t3.micro has 1 GiB of memory. Measured with the same images and data (Docker on WSL2, October 2026):

| Container | Memory while running (`docker stats`) | Highest seen |
| --- | --- | --- |
| db (postgres:16) | 47 MiB | 47 MiB (125 MiB with file cache, during the load) |
| rabbitmq (3.13) | 145-160 MiB | 210-260 MiB at start-up |
| web | 39 MiB | 40 MiB |
| worker | 37 MiB | 38 MiB (after one Update Analysis and one Pull Data) |
| init | exits | 346 MiB while it reads the 36 MB JSON file (peak RSS) |

So the four services need about 280 MiB, but `init` runs again on every `up -d` and briefly adds
346 MiB, which puts the containers above 600 MiB before Ubuntu and Docker themselves are counted.
A 1 GiB swap file gives that peak room, so the kernel does not have to kill a process to free
memory:

```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab     # keep it after a reboot
free -h                                                        # Swap: 1.0Gi
```

(A `t3.small` has 2 GiB and needs no swap; it is also in the free tier for accounts created on or
after July 15, 2025.)

## 6. Prepare the three files on the laptop

The server gets exactly three files. They are put together in a folder outside the repository, so the
real `.env` cannot be committed by accident. In WSL, at the top of the repository:

```bash
mkdir -p ~/gradcafe-ec2/data
cp module_7/ec2/docker-compose.ec2.yml ~/gradcafe-ec2/
cp module_7/ec2/.env.example ~/gradcafe-ec2/.env
cp module_6/src/data/applicant_data.json ~/gradcafe-ec2/data/
cd ~/gradcafe-ec2
for name in POSTGRES_PASSWORD RABBITMQ_DEFAULT_PASS WEB_DB_PASSWORD WORKER_DB_PASSWORD; do
  sed -i "s/^${name}=change-me\$/${name}=$(openssl rand -hex 24)/" .env
done
chmod 600 .env
grep '=change-me' .env || echo "no placeholders left"
docker compose -f docker-compose.ec2.yml --env-file .env config -q && echo "compose file OK"
```

Each password becomes 48 random hex characters. The last line checks the file with the laptop's
Docker; it prints `compose file OK` and starts nothing.

## 7. Copy them to the server

```bash
cd ~/gradcafe-ec2
ssh -i ~/.ssh/<KEY_NAME>.pem ubuntu@<EC2_PUBLIC_IPV4> 'mkdir -p ~/gradcafe/data'
scp -i ~/.ssh/<KEY_NAME>.pem docker-compose.ec2.yml .env ubuntu@<EC2_PUBLIC_IPV4>:~/gradcafe/
scp -i ~/.ssh/<KEY_NAME>.pem data/applicant_data.json ubuntu@<EC2_PUBLIC_IPV4>:~/gradcafe/data/
```

On the instance, check that all three arrived:

```bash
ls -la ~/gradcafe ~/gradcafe/data
sha256sum ~/gradcafe/data/applicant_data.json
# 473f969dd0dddf7d4adfc9f6e06c0d31d9d4ed464adf15e1fdb481bcf4732bae  (the same as module_6/src/data)
```

## 8. Start the stack

On the instance:

```bash
cd ~/gradcafe
docker compose -f docker-compose.ec2.yml --env-file .env pull
docker compose -f docker-compose.ec2.yml --env-file .env up -d
docker compose -f docker-compose.ec2.yml ps -a
```

`-f docker-compose.ec2.yml` is needed on every `docker compose` command here, because the file does not
have the default name `docker-compose.yml`. `ps -a` also lists `init`, which has finished, and its
PORTS column shows `0.0.0.0:8080->8080/tcp` for web and `127.0.0.1:15672->15672/tcp` for rabbitmq.
The STATUS column should read like this (from the local test run, printed with
`ps -a --format 'table {{.Service}}\t{{.Image}}\t{{.Status}}'`):

```text
SERVICE    IMAGE                            STATUS
db         postgres:16                      Up 25 seconds (healthy)
init       frametotoro/module_6:worker-v1   Exited (0) 18 seconds ago
rabbitmq   rabbitmq:3.13-management         Up 25 seconds (healthy)
web        frametotoro/module_6:web-v1      Up 18 seconds (healthy)
worker     frametotoro/module_6:worker-v1   Up 18 seconds
```

Screenshot of the `ps -a` output: `ec2-compose-ps.png`.

What happens on `up -d`: `db` and `rabbitmq` start and become healthy; `init` creates the tables and
the two database roles, loads the 30,500 applicants, computes the first analysis and exits 0; then `web`
and `worker` start. Locally this took under 10 seconds once the images were pulled; a t3.micro is slower.

## 9. Check that it works

**On the instance:**

```bash
curl -s http://127.0.0.1:8080/healthz; echo                    # ok
docker compose -f docker-compose.ec2.yml logs init
docker compose -f docker-compose.ec2.yml logs worker
```

`init` should end with `applicants table now holds 30,500 rows` and `Analysis snapshot computed`. The
worker should show `connected to RabbitMQ` and `waiting for tasks on queue tasks_q (prefetch_count=1)`.

**In the browser:** `http://<EC2_PUBLIC_IPV4>:8080`. Type the `http://`: the app has no HTTPS, and a
browser that tries `https://` first gets no answer. `/` redirects to `/analysis`. Screenshot with the
IP in the address bar: `ec2-app.png`.

**The two buttons (background tasks).** Each click queues a task and returns at once (HTTP 202); the
worker does the work. Follow the worker log while clicking:

```bash
docker compose -f docker-compose.ec2.yml logs -f --tail 5 worker      # Ctrl+C to stop following
```

* **Update Analysis**: the page says the task was queued and reloads once the new analysis is stored.
  Worker log (from the local test run):

  ```text
  delivery 1: running recompute_analytics, payload {}
  delivery 1: recompute_analytics committed in 0.3 s: {'total_entries': 30500}
  delivery 1 acknowledged (basic_ack) after the commit
  ```

* **Pull Data**: the worker checks Grad Café's robots.txt, reads the newest listing pages until it
  reaches entries it already has, inserts the new ones and recomputes the analysis. Worker log (from the
  local test run):

  ```text
  delivery 2: running scrape_new_data, payload {}
  delivery 2: scrape_new_data committed in 4.7 s: {'last_seen': 1020481, 'pages': 2, 'fetched': 38, 'inserted': 38, 'watermark': 1020519, 'complete': True, 'total_entries': 30538}
  delivery 2 acknowledged (basic_ack) after the commit
  ```

**On EC2 (2026-10-18)**, Update Analysis and then Pull Data in the browser at
`http://54.187.45.90:8080`; the worker log from `docker compose -f docker-compose.ec2.yml logs worker`
(the same lines are in `ec2-worker-log.png`):

```text
worker-1  | 2026-10-18 23:35:11,570 INFO gradcafe.worker: delivery 1: running recompute_analytics, payload {}
worker-1  | 2026-10-18 23:35:12,144 INFO gradcafe.worker: delivery 1: recompute_analytics committed in 0.6 s: {'total_entries': 30500}
worker-1  | 2026-10-18 23:35:12,144 INFO gradcafe.worker: delivery 1 acknowledged (basic_ack) after the commit
worker-1  | 2026-10-18 23:35:23,655 INFO gradcafe.worker: delivery 2: running scrape_new_data, payload {}
worker-1  | 2026-10-18 23:35:23,671 INFO gradcafe.scrape: Checking https://www.thegradcafe.com/robots.txt
worker-1  | 2026-10-18 23:35:23,970 INFO gradcafe.scrape: robots.txt (urllib.robotparser): survey=True result=True signin=False | (RFC 9309 longest-match): survey=True result=True signin=False
worker-1  | 2026-10-18 23:35:25,241 INFO gradcafe.scrape: new-entries page 1: 20 entries, 20 new (total new 20)
worker-1  | 2026-10-18 23:35:27,988 INFO gradcafe.scrape: new-entries page 2: 20 entries, 18 new (total new 38)
worker-1  | 2026-10-18 23:35:28,482 INFO gradcafe.worker: delivery 2: scrape_new_data committed in 4.8 s: {'last_seen': 1020481, 'pages': 2, 'fetched': 38, 'inserted': 38, 'watermark': 1020519, 'complete': True, 'total_entries': 30538}
worker-1  | 2026-10-18 23:35:28,482 INFO gradcafe.worker: delivery 2 acknowledged (basic_ack) after the commit
```

Both tasks went browser → web → RabbitMQ → worker → PostgreSQL on the instance, and each message was
acknowledged only after its commit. Grad Café answered the EC2 address (no Cloudflare block). The Pull
Data numbers equal the local test run's because both runs started from the same file (newest entry
1020481) and Grad Café's newest entry was 1020519 both times.

If Grad Café's Cloudflare protection refuses the EC2 address on another day, the log shows the task
delivered, run and rejected instead (section 11), which still shows the background path working.

**The RabbitMQ management UI, through an SSH tunnel.** In a second WSL terminal:

```bash
ssh -i ~/.ssh/<KEY_NAME>.pem -N -L 15672:127.0.0.1:15672 ubuntu@<EC2_PUBLIC_IPV4>
```

`-N` opens no shell; the command just holds the tunnel open until Ctrl+C. Open `http://localhost:15672`
in the Windows browser and log in with `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS` from `.env`
(`guest` is refused). **Queues and Streams** shows `tasks_q` with 1 consumer. If the local Module 6
stack is using port 15672, tunnel another local port instead: `-L 15673:127.0.0.1:15672` and
`http://localhost:15673`.

Checked on 2026-10-18 through a tunnel (local port 15679) with the management API: `tasks_q` durable,
1 consumer, 0 messages ready, 0 unacknowledged, 2 acknowledged; `guest:guest` gets HTTP 401.

**From outside, only 22 and 8080 answer.** From the laptop, both of these should time out:

```bash
nc -zv -w 5 <EC2_PUBLIC_IPV4> 5432
nc -zv -w 5 <EC2_PUBLIC_IPV4> 15672
```

On 2026-10-18 from the laptop: 22 connected, `curl http://54.187.45.90:8080/healthz` returned HTTP 200,
and 5432, 5672 and 15672 timed out.

## 10. Stop the instance

EC2 console > the instance > **Instance state > Stop instance**. Stop, do not terminate: Module 8 uses
the instance again.

* The containers come back on the next start by themselves: Docker starts at boot, and the services
  have `restart: unless-stopped`. The data is in the named volumes.
* The public IPv4 address changes after every stop and start. Use the new one for SSH and the browser.
* A stopped instance is not billed for compute; its EBS volume still is ($0.08 per GB-month for gp3,
  about $1.28 a month for 16 GiB).

Commands that are useful later:

```bash
docker compose -f docker-compose.ec2.yml down       # stop and remove the containers; volumes stay
docker compose -f docker-compose.ec2.yml down -v    # also delete the database and the queues
```

## 11. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `E: Unable to locate package docker-compose-plugin` | Ubuntu calls it `docker-compose-v2` (step 4). |
| A purple "Daemons using outdated libraries" screen during `apt-get install` | Ubuntu's needrestart asking which services to restart. Press Enter to accept the default. |
| `permission denied while trying to connect to the Docker daemon socket` | The `docker` group applies only after logging in again (or `newgrp docker`). `groups` should list `docker`. |
| `WARNING: UNPROTECTED PRIVATE KEY FILE!` / `bad permissions` | The key is still under `/mnt/c`, or not `chmod 400`. Copy it to `~/.ssh` (step 3). |
| `Permission denied (publickey)` | Wrong user (`ubuntu` on Ubuntu, `ec2-user` on Amazon Linux) or the wrong `.pem` file. |
| `ssh` or the page times out | The security group's source is not my current IP. Edit both rules to **My IP**. Also check that the instance is running and has a public IPv4. |
| The page does not load but `curl http://127.0.0.1:8080/healthz` on the instance says `ok` | The 8080 rule is missing, or the browser tried `https://`. Type `http://<EC2_PUBLIC_IPV4>:8080`. |
| `no configuration file provided: not found` | `-f docker-compose.ec2.yml` is missing from the command, or the command was not run in `~/gradcafe`. |
| `required variable <NAME> is missing a value: set <NAME> in .env (see .env.example)` | `.env` is not in `~/gradcafe` (`ls -la` shows dot files), or the `<NAME>` line is missing. With no `.env` at all, Compose names `RABBITMQ_DEFAULT_USER` first (`error while interpolating x-rabbitmq-url: ...`). |
| `bind source path does not exist: /home/ubuntu/gradcafe/data/applicant_data.json` | The data file was not copied (step 7). Compose refuses to start `init` rather than mount an empty folder. |
| `service "init" didn't complete successfully: exit 1` (or 2, 3) | `docker compose -f docker-compose.ec2.yml logs init` says why: 1 a setting or the data file is unusable, 2 it cannot connect to `db`, 3 a statement failed (nothing was changed). |
| `no matching manifest for linux/arm64` or `exec format error` | An ARM instance (`t4g`) or an ARM AMI. The images are x86 only: use `t3.micro` and the 64-bit (x86) AMI. |
| Containers restart, `up -d` hangs, or `dmesg` shows `Out of memory: Killed process` | Not enough memory: add the swap file (step 5), or use a `t3.small`. |
| The RabbitMQ UI or `init` rejects a password after `.env` was changed | PostgreSQL and RabbitMQ keep the passwords from their first start. `down -v` (this deletes the data), then `up -d`; `init` loads the data again. |
| `no space left on device` | `df -h /`. Old images: `docker image prune`. Or enlarge the volume in the console (**Volumes > Modify volume**), then grow the file system on the instance (`sudo growpart /dev/nvme0n1 1` and `sudo resize2fs /dev/nvme0n1p1`, as `lsblk` names them). |
| Pull Data: the worker log shows `scrape_new_data failed and was rolled back: ScrapeBlockedError: HTTP 403 ...` (or `Cloudflare mitigation`, `challenge page`), then `rejected (basic_nack, requeue=False)` | Grad Café's Cloudflare refused the EC2 address (it may treat a data-center address more strictly than a home connection). The deployment is fine: the task was queued, delivered, run, rolled back and rejected. Record the log lines in section 9; Update Analysis still shows the background path. Try once more later; do not click repeatedly. |
| `localhost:15672` shows the local Module 6 RabbitMQ instead of the EC2 one | Port 15672 on the laptop is taken; tunnel 15673 instead (step 9). |
