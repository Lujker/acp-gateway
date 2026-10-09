# VPS, two computers and Telegram acceptance

This runbook prepares the first live deployment: the **Gateway** runs on the
VPS in Docker; an outgoing **connector** runs on each Linux/WSL computer;
Goose runs locally on both computers. Start with a direct IP/port connection,
then switch to `wss://akv-server.com/acpgw/connect` through the existing mirror.
Hermes as an ACP agent is a later interoperability check.

```text
Telegram ──> VPS Gateway ──> home/goose
                      └──> work/goose
                 ▲
    outbound connectors from each computer
```

## 1. Prepare the VPS checkout

Clone alongside AKV, in a separate directory, for example `~/acp-gateway`.
Requirements: Docker Engine with Compose v2+, git, Python 3 for the small
preparation script. Runtime Python and application dependencies are in the image.

From the ACP Gateway checkout:

```bash
python3 deploy/docker/prepare.py
cd deploy/docker
```

Preparation creates private `runtime/` (configuration/tokens), `state/` (SQLite,
pins and enrollment exports), and `.env` (Compose UID/GID). Existing files are
kept. Run it as the user who will own this deployment; the container uses that
UID/GID. These paths are ignored by Git and excluded from the image build.
Store them on the Linux filesystem rather than a Windows-mounted directory.

Edit `runtime/gateway.yaml`:

- Set the **actual absolute Goose working directory** separately for `home`
  and `work`; it must exist on the respective computer.
- Keep aliases `home/goose` and `work/goose`, `session_mode: approve`, and
  `connector.connect_path: /acpgw/connect` for the first acceptance.
- Set `telegram.enabled: true` and `allowed_user_ids: [YOUR_NUMERIC_USER_ID]`.

Edit `runtime/gateway.env` and set `TELEGRAM_BOT_TOKEN` to a dedicated bot's token.
Owner API and MCP tokens were generated independently; leave them private.
The VPS does not need either Goose secret. Only one process may poll this bot;
stop any development Gateway polling the same token before enabling this one.
Existing webhooks must be removed explicitly; see [Telegram setup](telegram.md).

The AKV controller uses an external Docker network named `app-network`.
Check its actual name and that `nginx_controller` belongs to it:

```bash
docker inspect nginx_controller --format '{{json .NetworkSettings.Networks}}'
docker network inspect app-network --format '{{.Name}}'
```

If the deployed name differs, add `ACPGW_PROXY_NETWORK=ACTUAL_NAME` to
`deploy/docker/.env`. Do not create or replace AKV's existing network.
Gateway uses a distinct Compose project (`acpgw`), its own SQLite and bounded
resources, no published 80/443 ports, no Docker socket and no AKV volumes.

```bash
docker compose build
docker compose run --rm gateway config check
```

## 2. First stage: direct IP and separate port

Add to `deploy/docker/.env`:

```dotenv
ACPGW_INGRESS_BIND=0.0.0.0
ACPGW_CONNECTOR_PORT=18766
```

Check the port is free (`ss -ltn sport = :18766`) before starting. The optional
override publishes **only connector ingress**, never owner API/MCP:

```bash
docker compose -f compose.yaml -f compose.direct.yaml config --quiet
docker compose -f compose.yaml -f compose.direct.yaml up -d
docker compose exec gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env status
docker compose logs --tail 50 gateway
```

Allow TCP 18766 from the two test computers in the server's existing firewall
policy, including Docker forwarding rules if used. Do not reset the firewall
or run an AKV deployment to expose this port. AKV's mirror-only rules on
80/443 stay unchanged. The first URL is:

```text
ws://VPS_IP:18766/acpgw/connect
```

This first stage uses the owner's selected plaintext test transport: computer
credentials, prompts and answers on that link are unencrypted. Use harmless
test prompts; the domain stage below replaces it with WSS.

Enroll two stable identities on the VPS (new credential files are required):

```bash
docker compose exec gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env computers enroll home --name "Home computer" --token-file /data/enrollment/home.key
docker compose exec gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env computers enroll work --name "Work computer" --token-file /data/enrollment/work.key
```

Securely copy `state/enrollment/home.key` to the home computer and `work.key`
to the work computer, for example over your existing SSH/SCP connection.
Each computer receives only its own key. Keep the file locally owned, mode
`0600`, in a directory with mode `0700`; symlinks are rejected.

## 3. Start Goose and the connectors on both computers

Use Linux/WSL on both computers for this acceptance. Clone the repository,
run `uv sync --frozen`, and create a private configuration directory:

```bash
install -d -m 700 ~/.config/acp-gateway-connector
cp deploy/docker/computer.example.yaml ~/.config/acp-gateway-connector/computer.yaml
```

Set `url` to the **existing local Goose server**, its exact TLS fingerprint,
and the same `default_cwd` / `session_mode: approve` as the respective VPS route.
The template uses `wss://127.0.0.1:3284/acp`; adjust its port to the actual
server. If Goose already runs as a service, use it rather than starting a
second server. [Goose setup](work-goose.md) explains its server mode.

Create `~/.config/acp-gateway-connector/computer.env`, mode `0600`, containing
`LOCAL_GOOSE_SECRET=...` with that local server's secret. Place the transferred
enrollment key beside it as `computer.key`, also mode `0600`. The connector
does not need the VPS owner/MCP/Telegram tokens.

On the home computer, from its checkout:

```bash
uv run acpgw --config ~/.config/acp-gateway-connector/computer.yaml --env-file ~/.config/acp-gateway-connector/computer.env config check
uv run acpgw --config ~/.config/acp-gateway-connector/computer.yaml --env-file ~/.config/acp-gateway-connector/computer.env connector --dispatcher-url ws://VPS_IP:18766/acpgw/connect --computer-id home --token-file ~/.config/acp-gateway-connector/computer.key
```

On the work computer run the same commands with `--computer-id work`.
Keep them in foreground until acceptance passes. A second connector using
the same computer identity replaces the first; use different identities for
the two computers.

On the VPS:

```bash
docker compose exec gateway acpgw --config /config/gateway.yaml --env-file /config/gateway.env computers status
```

Expect both computers online and their `goose` aliases advertised. An agent
can initially show `agent_not_initialized`; its ACP connection opens on the
first request. After a successful prompt, expect `agent_ready: true`.
`online` alone does not prove Goose authentication/cwd/mode is correct.

## 4. Telegram acceptance

Open the dedicated bot's private chat; allow a few seconds between commands
(the channel permits five messages per ten seconds).

1. Send `/start`, `/agent`. Both `home/goose` and `work/goose` should be listed.
2. `/agent home/goose`, `/new`, then “Remember the code word AMBER. Answer OK.”
3. `/agent work/goose`, `/new`, then “Remember the code word COBALT. Answer OK.”
4. Switch between the two with `/agent ...` and ask for the code word. Expect
   AMBER at home and COBALT at work. `/sessions` lists only the selected route.
5. On each route request a harmless tool action: “Run exactly this command:
   `echo ACPGW_ACCEPTANCE`.” First reject the card, then repeat and allow once.
   The card must identify the correct `home/goose` or `work/goose`; the rejected
   action must not execute. Check the accepted result on the relevant computer.
6. Ask for `sleep 30`, approve it, then send `/stop`. The Gateway job should
   cancel. Whether Goose kills its already running child process is a separate
   agent behavior; inspect it locally before drawing that conclusion.
7. Stop the work connector with Ctrl+C. A work request should fail; a home
   request should still succeed. Restart the work connector and check its code
   word again. Restore requires Goose's `session/load` support and saved sessions.
8. With no jobs running, restart **only** this Gateway:
   `docker compose restart gateway`. Check connectors reconnect, `/agent`,
   `/sessions` and the remembered words still work.

Save the `Working: JOB_ID` identifier when testing interruptions. `/result JOB_ID`
retrieves a stored result after a missed delivery; `/approvals` redisplays pending
cards. Do not automatically resubmit a tool action interrupted by a network
failure: it may have executed already. Offline requests are not queued, and
durable recovery of an uncertain result is still future work. A Gateway restart
does not resume in-flight jobs automatically. Final answers are separate messages;
streaming edits are not an acceptance requirement for this stage.

## 5. Second stage: akv-server.com through its mirror

The inspected AKV setup has an L4/SNI mirror: it forwards TLS bytes to origin
443 or 17443 with PROXY protocol. TLS terminates at `nginx_controller`; the
mirror does not route HTTP paths. Consequently the new location belongs only
in the **origin controller's HTTPS server block**. Mirror DNS/SNI/firewall,
existing certificates and all existing `/vpn/`, `/node-deployer/` and other
locations can stay as configured. Verify the deployed setup matches this
checkout before applying the change.

Use [the prepared location](../../deploy/docker/nginx-location.conf.example)
inside the existing `server { listen 443 ssl; ... }` block in
`akv-vpn-controller/nginx/nginx.conf`. That block also serves 17443. Keep the
exact path `/acpgw/connect` and domain guard `akv-server.com`. The existing
Docker resolver and `$connection_upgrade` map are reused. The variable upstream
is resolved at request time, so an absent Gateway cannot prevent nginx reload.
Owner API, MCP and health endpoints are not proxied.

Prepare and review a **candidate copy** rather than editing the running file
first. From the controller checkout, with `nginx.candidate.conf` containing
the complete config plus the one location:

```bash
diff -u nginx/nginx.conf nginx.candidate.conf
docker cp nginx.candidate.conf nginx_controller:/tmp/acpgw-candidate.conf
docker exec nginx_controller nginx -t -c /tmp/acpgw-candidate.conf
```

Record the current responses of `/`, `/vpn/health` and `/node-deployer/` through
`https://akv-server.com` before applying; compare after reload. Keep a private
backup of the original config. Once the candidate passes, update the mounted
file **in place** and reload, without rebuilding/restarting the AKV stack:

```bash
cp -p nginx/nginx.conf nginx.before-acpgw.conf
cat nginx.candidate.conf > nginx/nginx.conf
docker exec nginx_controller nginx -t
docker exec nginx_controller nginx -s reload
```

Writing in place preserves the inode of the file already bind-mounted by AKV;
do not replace it with `mv`. If validation or the existing-route checks fail,
restore the backup in place, validate and reload again. Preserve the reviewed
location in the controller's source configuration for subsequent AKV deployments.
Do not run `setup.sh`, `compose down`, change controller images, or touch its DB
to add this route.

Restart each foreground connector with only the URL changed to:

```text
wss://akv-server.com/acpgw/connect
```

Use normal CA/hostname verification for the domain's public certificate; no
connector pin argument is needed. Repeat the Telegram tests, including an idle
connection longer than two minutes and one computer disconnecting. In VPS logs,
both computers should stay online. A response to a plain browser GET does not
prove the authenticated WebSocket/ACP route works.

Once both computers work over WSS, remove the temporary IP-port exposure:

```bash
# From ACP Gateway's deploy/docker, with no active jobs:
docker compose -f compose.yaml up -d
docker compose ps
```

Remove the temporary 18766 firewall allowance through the existing firewall
management path. This recreation briefly disconnects the connectors; they
should reconnect to WSS. AKV containers are not part of this Compose project.
After acceptance, install the local connector services using
[the connector service instructions](service.md#separate-computer-connector-service).

## Automated preparation evidence

Build locally and run the opt-in isolated smoke:

```bash
docker build -t acpgw:local .
uv run python scripts/smoke_docker.py --image acpgw:local
```

It exercises the actual Docker runtime/Compose configuration, direct IP/port,
two independent mock Goose agents, exact nginx path, WSS, a separate L4 mirror
with PROXY protocol, invalid credentials, disconnect isolation and session load.
It removes only its temporary containers/network and uses no real bot or AKV
services. The automated Telegram relay tests use real aiogram types with a fake
Bot API session; real Telegram delivery and physical hosts remain the live test.

The nginx WebSocket directives follow the
[official nginx documentation](https://nginx.org/en/docs/http/websocket.html);
the shared external network follows
[Docker Compose network semantics](https://docs.docker.com/reference/compose-file/networks/).
