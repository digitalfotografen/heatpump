# heatpump

Price-controlled pausing of a Rego 637 heat pump. A Raspberry Pi with a custom HAT closes a
relay on the pump's EXT input when electricity is expensive or the wood stove is burning.
See [docs/plan.md](docs/plan.md) for the design and the phased implementation plan.

## Development

```sh
uv sync
uv run heatpump --help
uv run pytest
uv run ruff format . && uv run ruff check .
```

Copy `config.example.yaml` to `config.yaml` (git-ignored). On a machine without the HAT set
`relay.driver: dry-run` and `heating.enabled: false`. Secrets are read from the environment:
`TIBBER_TOKEN`, `MQTT_USER`, `MQTT_PASSWORD` (the variable names are set in the config).

## Deploying to the Raspberry Pi

One-time setup on the Pi:

```sh
sudo apt install i2c-tools
sudo usermod -aG i2c pi            # log out and in again afterwards
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.13                # only if the system Python is older
sudo install -d -o pi -g pi /var/lib/heatpump /etc/heatpump
sudo install -m 0600 -o pi /dev/null /etc/heatpump/env    # TIBBER_TOKEN=..., MQTT_USER=..., MQTT_PASSWORD=...
i2cdetect -y 1                        # expect 0x22 (MCP23017) and 0x66 (MCP9600)
```

Every deploy, from the development machine (never `--delete`, and keep the Pi's `.venv`):

```sh
rsync -av --exclude .venv --exclude .git --exclude .idea --exclude __pycache__ \
    --exclude config.yaml ./ pi@PI:heatpump/
ssh pi@PI 'cd heatpump && uv sync --no-dev && uv run heatpump --help'
```

Put the configuration in `/etc/heatpump/config.yaml` (start from `config.example.yaml`).

### Running the controller by hand

```sh
set -a; . /etc/heatpump/env; set +a
heatpump --config /etc/heatpump/config.yaml run        # foreground; Ctrl-C or SIGTERM releases the relay
```

While it runs, from another shell: `heatpump status [--json]`, `heatpump pauses`,
`heatpump log --tail 20` and `heatpump export control_log --from 2026-10-21 --format csv`.
`relay set` is refused while the service holds the lock file.

### As a service

The unit runs `heatpump --config /etc/heatpump/config.yaml run` as user `pi` with
`EnvironmentFile=/etc/heatpump/env`, `Restart=always` and `RestartSec=10`. Its template is
`src/heatpump/templates/heatpump.service`; `service install` fills in the user, the venv's
`heatpump` script, the config path and the env file:

```sh
cd ~/heatpump
sudo .venv/bin/heatpump --config /etc/heatpump/config.yaml service install   # --print to preview
sudo systemctl daemon-reload && sudo systemctl enable --now heatpump
heatpump service status
journalctl -u heatpump -f
```

Run `service install` again after moving the project or changing the config path.

### Replaying a day

`heatpump replay --date 2026-10-10` runs the planner over the stored prices of that local day,
re-planning every `replan_interval_hours` with the pauses already taken as history, and prints
the price pauses and the expected saving. It reads the database only; it never touches the
relay, and stove pauses are not simulated.

### Soak test (before phase 12)

With the service running and the relay not wired to EXT, check after a few days:
`heatpump pauses`, `heatpump log --since 2026-10-21`, `heatpump heating`, and
`heatpump export control_log --from ... --format csv` for plots. Also try `sudo reboot`
(the service must come back and `relay status` must say running right after boot) and stop the
MQTT broker for a while (the stove pauses must end through the guard, with an alert, and resume
once the broker is back).

The relay is **not** connected to the heat pump's EXT input until the commissioning step in
phase 12 of the plan.
