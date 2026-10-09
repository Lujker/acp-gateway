# VPS, two computers and Telegram acceptance

[Русская версия](vps-acceptance.ru.md)

This runbook prepares the first live deployment: the **Gateway** runs on the
VPS in Docker; an outgoing **connector** runs on each Linux/WSL computer;
Goose runs locally on both computers. Start with a direct IP/port connection,
then switch to `wss://gateway.example.com/acpgw/connect` through your own nginx.
Hermes as an ACP agent is a later interoperability check.

```text
Telegram ──> VPS Gateway ──> home/goose
                      └──> work/goose
                 ▲
    outbound connectors from each computer
```

## 1. Prepare the VPS deployment

Extract a reviewed release bundle into a separate directory, for example
`~/acp-gateway`. See [installation and updates](distribution.md) for artifacts
and publication status. A Git clone is the separate source-build alternative.
Requirements: Docker Engine with Compose v2+ and Python 3 for the small
preparation script; Git is needed only for the source-build alternative. Runtime Python and application dependencies are in the image.

From the extracted bundle or ACP Gateway checkout:

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

Gateway uses a distinct Compose project (`acpgw`), its own managed Docker network,
SQLite and bounded resources. The base configuration publishes no host ports.
It does not require any existing reverse proxy or another application's network.
Preparation also creates `runtime/nginx.conf` and `runtime/tls/` for stage 2.

```bash
if [ -f ../../gateway-image.tar ]; then
  docker load -i ../../gateway-image.tar
elif [ -f ../../release.json ]; then
  docker compose pull
else
  docker compose build
fi
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
or change unrelated services to expose this port. The first URL is:

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

Use Linux/WSL on both computers for this acceptance. Install the tool using
[the release instructions](distribution.md); from the extracted bundle, create
a private connector configuration directory. A Git checkout with
`sh scripts/install.sh` is the separate alternative:

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

On the home computer, from any directory:

```bash
acpgw --config ~/.config/acp-gateway-connector/computer.yaml --env-file ~/.config/acp-gateway-connector/computer.env config check
acpgw --config ~/.config/acp-gateway-connector/computer.yaml --env-file ~/.config/acp-gateway-connector/computer.env connector --dispatcher-url ws://VPS_IP:18766/acpgw/connect --computer-id home --token-file ~/.config/acp-gateway-connector/computer.key
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

## 5. Second stage: domain and your own nginx

Point your domain's DNS record at the VPS IP. `gateway.example.com` is a
placeholder: replace it with your own domain in `runtime/nginx.conf` and in
connector URLs. Obtain a trusted TLS certificate using your certificate provider
or ACME client; certificate issuance/renewal is managed separately from this stack.
Place its full chain in `runtime/tls/fullchain.pem` and private key in
`runtime/tls/privkey.pem`. Keep the key private (`chmod 600`).

The optional [nginx Compose file](../../deploy/docker/compose.nginx.yaml) starts
a dedicated nginx on the project's network. It exposes HTTPS port 443; no HTTP
port is required by this configuration. Check that 443 is free (`ss -ltn sport = :443`).
If another service owns it, choose a free port with `ACPGW_HTTPS_PORT=18443` in
`.env` and include `:18443` in connector URLs. Do not stop another service to free
its port. Allow the selected HTTPS port in your firewall policy.

The [nginx configuration](../../deploy/docker/nginx.conf.example) includes
[the exact connector location](../../deploy/docker/nginx-location.conf.example).
It preserves `/acpgw/connect` and WebSocket/authentication headers. Docker DNS is
resolved at request time, so an absent Gateway cannot prevent nginx from starting.
Owner API and MCP are not proxied; other paths return 404.

Keep the direct test port available while validating HTTPS:

```bash
docker compose -f compose.yaml -f compose.direct.yaml -f compose.nginx.yaml config --quiet
docker compose -f compose.yaml -f compose.direct.yaml -f compose.nginx.yaml run --rm --no-deps nginx nginx -t
docker compose -f compose.yaml -f compose.direct.yaml -f compose.nginx.yaml up -d
docker compose -f compose.yaml -f compose.direct.yaml -f compose.nginx.yaml logs --tail 50 nginx
```

After certificate renewal or configuration edits, validate and reload nginx:

Edit the mounted `runtime/nginx.conf` in place. If your editor replaces the file
instead, recreate the nginx container with the same Compose files and
`up -d --force-recreate nginx` so it mounts the new file before validation.

```bash
docker compose -f compose.yaml -f compose.nginx.yaml exec nginx nginx -t
docker compose -f compose.yaml -f compose.nginx.yaml exec nginx nginx -s reload
```

Restart each foreground connector with only the URL changed to:

```text
wss://gateway.example.com/acpgw/connect
```

Use normal CA/hostname verification for the domain's public certificate; no
connector pin argument is needed. Repeat the Telegram tests, including an idle
connection longer than two minutes and one computer disconnecting. In VPS logs,
both computers should stay online. A response to a plain browser GET does not
prove the authenticated WebSocket/ACP route works.

Once both computers work over WSS, remove the temporary IP-port exposure:

```bash
# From the deployment's deploy/docker, with no active jobs:
docker compose -f compose.yaml -f compose.nginx.yaml up -d
docker compose -f compose.yaml -f compose.nginx.yaml ps
```

Remove the temporary 18766 firewall allowance through the existing firewall
management path. This recreation briefly disconnects the connectors; they
should reconnect to WSS. This Compose project owns only its Gateway, nginx and network.
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
It removes only its temporary containers/network and uses no real bot or production
services. The automated Telegram relay tests use real aiogram types with a fake
Bot API session; real Telegram delivery and physical hosts remain the live test.

The nginx WebSocket directives follow the
[official nginx documentation](https://nginx.org/en/docs/http/websocket.html);
the project network follows
[Docker Compose network semantics](https://docs.docker.com/reference/compose-file/networks/).
