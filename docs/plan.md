# Heat pump price controller: implementation plan

Repo: `heatpump`, deployed to the Raspberry Pi over ssh. Developed in PyCharm with a Claude Code agent; the agent works one phase at a time and **commits only after the developer has reviewed the phase**. Conventions (uv, YAML + pydantic config, typer CLI, SQLite, systemd service, readability rules) are copied from `mcp9600_logger/agents.md`; `fridge` is the reference for the Tibber client, planner/controller split and MQTT publishing.

Each phase is sized for roughly one hour of agent work and ends with something runnable or testable.

**Hardware situation (2026-10-20):** the controller runs on its own Raspberry Pi with the custom HAT. On the HAT's I2C bus: an **MCP23017** at `0x22` whose **GPB0** drives the EXT relay, and an **MCP9600** at `0x66` with a type K thermocouple on the **heating circuit**. The relay is wired and audible but **not yet connected to the heat pump's EXT input**, so the real relay driver can be used from the start without any effect on the house. Connecting EXT is a deliberate commissioning step (phase 12) that happens only after the controller has run for several days and its log has been reviewed. The wood stove is monitored by the other Pi (mcp9600_logger), which publishes to MQTT; nothing changes there.

Background documents (read before starting): `Presentationer/Pycon2026/claude/price_control_findings.md` (the simulation study; `heatpump_models.py` next to it holds `pick_pauses_with_hold` and `follows_pause_rules`, which the planner ports), and the `HeatPump_plan` research docs for the Rego EXT input.

---

## 1. What the controller does

The Rego 637 pauses the heat pump, including hot water, when the EXT input is closed. The relay on EXT is driven by MCP23017 GPB0 (I2C `0x22`): **GPIO high = relay closed = heat pump paused**. The controller decides, once per minute, whether the pump should be paused right now, for one of two reasons:

| Reason | Trigger | Rules |
|---|---|---|
| **Price pause** | The planner has put this quarter in a pause | ≥ 30 min, ≤ 3 h per pause, ≤ 6 h per rolling 24 h, recovery after a pause = pause / 2 but ≥ 30 min, only when the plan's expected saving is above a minimum, never when prices for the current quarter are missing |
| **Stove pause** | Stove exhaust temperature ≥ 50 °C (configurable), ends at ≤ 40 °C (threshold − hysteresis) | ≥ 30 min, ≤ 6 h in a row, then recovery = pause / 2 (≥ 30 min), after which it may pause again if still burning. Not charged to the price budget. Never when the stove feed is stale. |

A stove pause overrides the price plan. While the stove burns the planner is re-run every hour so that the price budget is spent on the next-best peaks instead of the one the stove already covers. Pausing is allowed all year: in summer the pump only makes hot water, and blocking that in price peaks is wanted.

A **safety guard** sits between the decision and the relay and can only ever *release* the relay. It has the last word.

## 2. Architecture

The repo already has a `uv init` src layout (`src/heatpump/`, Python 3.13 pinned in `.python-version`, `heatpump = "heatpump:main"` script entry). Keep that layout.

```
src/heatpump/
  __init__.py      exposes main() for the script entry
  config.py        YAML → pydantic Settings
  db.py            SQLite schema and simple repository functions
  models.py        Quarter, Pause, Plan, StoveState, ControlDecision (dataclasses)
  prices.py        Tibber fetch (pyTibber + tenacity), store, "when to fetch" rule
  weather.py       Open-Meteo outdoor temperature
  stove.py         MQTT subscriber + hysteresis state machine + staleness
  planner.py       Pure function: prices + constraints + history → Plan
  budget.py        Rolling-24 h pause budget and recovery bookkeeping from pause history
  guard.py         SafetyGuard: the rules that may force "run"
  i2c.py           One shared smbus2 bus handle and a tiny register read/write helper
  mcp23017.py      MCP23017 driver: configure one pin as output, set, read back
  mcp9600.py       MCP9600 driver, ported from mcp9600_logger (hot-junction temperature, status)
  relay.py         Relay protocol, DryRunRelay (tests, dev machine), Mcp23017Relay (the Pi)
  heating.py       Heating-circuit temperature sampler (MCP9600 → database → MQTT)
  controller.py    1-minute tick: combine plan, stove, guard → relay; writes control_log
  mqtt.py          Publisher (aiomqtt), command topics
  cli.py           typer: run, fetch-prices, plan, status, replay
  __main__.py
config.example.yaml  (already in the repo; keep it in sync with config.py)
systemd/heatpump.service
tests/
docs/plan.md  (this file)
```

Principles the agent must keep:

- **Planner and guard are pure functions** with no I/O. All I/O lives in `prices`, `weather`, `stove`, `mqtt`, `db`, `relay`. This is what makes the code testable and presentable in a talk.
- **One asyncio loop.** pyTibber and aiomqtt are async; the controller tick is an asyncio task; sqlite3 calls are short and run inline.
- **UTC everywhere in storage**; Europe/Stockholm only when printing or deciding "after 14:00 local". Quarters are keyed by their UTC start time as ISO strings.
- The relay is only ever touched by `controller.py` (through `guard.py`) and by `relay set` on the command line, which refuses while the service runs.
- **Hardware drivers are thin and own no policy.** `mcp23017.py` and `mcp9600.py` talk registers; everything that decides anything lives above `relay.py` and `heating.py`. On the development machine, `driver: dry-run` and `heating.enabled: false` make the whole application run without the HAT.
- Readability over cleverness: short functions, named constants, docstrings that say *why*. No framework magic.

## 3. Configuration (`config.example.yaml`)

```yaml
location:
  latitude: 59.33      # Stockholm, verify
  longitude: 18.07
  timezone: Europe/Stockholm

tibber:
  token_env: TIBBER_TOKEN        # read from the environment, never from the file
  home_index: 0
  fetch_after_local_time: "14:00"
  fetch_give_up_local_time: "23:00"

price_pause:
  min_minutes: 30
  max_minutes: 180
  budget_minutes_per_24h: 360
  recovery_fraction: 0.5         # recovery = pause * fraction
  recovery_min_minutes: 30
  min_expected_saving_sek: 1.0   # per plan horizon; below this, no price pauses
  heat_demand_kw: 1.0            # flat demand used for scoring until outdoor-based demand exists
  replan_interval_hours: 6
  replan_interval_while_burning_hours: 1

stove:
  temperature_topic: stove/temperature
  availability_topic: stove/availability
  burning_above_c: 50
  hysteresis_c: 10
  min_pause_minutes: 30
  max_pause_minutes: 360
  recovery_fraction: 0.5
  recovery_min_minutes: 30
  stale_after_seconds: 300

weather:
  provider: open-meteo
  fetch_interval_minutes: 30
  stale_after_minutes: 180

guard:
  absolute_max_pause_minutes: 360
  max_paused_minutes_per_24h: 720   # price + stove combined; keeps a hot water window

relay:
  driver: mcp23017                  # mcp23017 on the Pi | dry-run on the dev machine and in tests
  i2c_bus: 1
  i2c_address: 0x22
  pin: GPB0                         # high = relay closed = EXT closed = heat pump paused

heating:
  enabled: true                     # false on the dev machine
  i2c_bus: 1
  i2c_address: 0x66
  thermocouple_type: K
  sample_interval_seconds: 60

mqtt:
  host: 192.168.1.x
  port: 1883
  username_env: MQTT_USER
  password_env: MQTT_PASSWORD
  topic_prefix: heatpump

database:
  path: /var/lib/heatpump/heatpump.sqlite3

control:
  tick_seconds: 60
  lock_file: /var/lib/heatpump/heatpump.lock   # refuses manual relay commands while the service runs
  log_level: INFO
```

## 4. Database

SQLite, WAL mode, rows kept forever. All timestamps ISO 8601 UTC.

| Table | Columns | Notes |
|---|---|---|
| `prices` | `slot_start` PK, `slot_minutes`, `energy`, `tax`, `total`, `currency`, `fetched_at` | One row per quarter. Re-fetch upserts. |
| `stove_readings` | `ts` PK, `temperature`, `state` | Every MQTT message (1/min). |
| `outdoor_readings` | `ts` PK, `temperature`, `source` | |
| `heating_readings` | `ts` PK, `temperature`, `cold_junction` | Heating-circuit thermocouple, 1/min. Monitoring only for now; later the basis for seeing how fast the circuit cools during a pause and recovers after it. |
| `plans` | `id`, `created_at`, `reason`, `horizon_start`, `horizon_end`, `expected_saving_sek`, `pauses_json` | Every planner run, even "no pauses". |
| `pause_events` | `id`, `kind` (price/stove), `started_at`, `planned_end`, `ended_at`, `end_reason` | The source for budget and recovery bookkeeping. |
| `control_log` | `ts` PK, `mode`, `relay_requested`, `relay_actual`, `guard_reason`, `stove_state`, `stove_temp`, `outdoor_temp`, `heating_temp`, `price_now`, `plan_id` | One row per tick (1 min). `relay_actual` is read back from the chip. |

## 5. The planner (`planner.py`)

Input: `quarters` (sorted list of `Quarter(start, price)` from *now* rounded down to the quarter, to the end of known prices; `price` = Tibber `energy`), `history` (pause events from the last 24 h), `now`, `stove_pause` (the current stove pause, if any, as an already-paused interval), `settings`. Output: `Plan(pauses: list[Pause], expected_saving_sek, reason)`.

Algorithm, a port of model 2b with model 3's rules and the lessons from §5 of the findings:

1. **Budget left** = `budget_minutes_per_24h` − price-paused minutes in the last 24 h (from history), counted on a rolling window that moves across the horizon.
2. **Blocked quarters**: the current stove pause interval and its recovery window afterwards; the recovery window of the last price pause; quarters without a price.
3. **Greedy growth** (`pick_pauses_with_hold`): walk quarters from most to least expensive; a quarter adjacent to an existing pause extends it, otherwise start a new pause from the most expensive 2-quarter window containing it. Reject any step that breaks `follows_pause_rules`: min 2 quarters, max 12, recovery gap ≥ max(2, len / 2) quarters after every pause, budget not exceeded.
4. **Score each pause by ne/t saving**: `demand_kw × 0.25 h × (Σ price during pause − Σ price during catch-up)`, where catch-up is the same number of quarters immediately after the pause. Drop pauses with net saving ≤ 0 and give their budget back to the next candidate. This is what removes the "late evening pause paid back at tomorrow's morning price" losses.
5. If the total expected saving of the horizon is below `min_expected_saving_sek`, return an empty plan with reason `flat-day`.
6. A price pause that is **already in progress is kept** until its minimum length; it may be shortened or extended by the new plan beyond that.

Re-plan triggers (in `controller.py`): startup, new prices stored, stove state change (either direction), hourly while the stove is burning, every `replan_interval_hours` otherwise, and the `replan` command topic.

## 6. The guard (`guard.py`)

`decide(requested: bool, state) -> (relay: bool, reason: str)`. The guard may only turn a `True` into `False`. It forces **run** when:

- the pump has been paused for longer than `absolute_max_pause_minutes` in a row, regardless of reason
- total paused minutes in the last 24 h exceed `max_paused_minutes_per_24h`
- a price pause is requested and the current quarter has no price
- a stove pause is requested and the stove feed is stale or offline
- the system clock is implausible (before the build date, or jumped by more than an hour since the last tick)
- `force_run` has been set via MQTT (latched until `force_run` is cleared)
- the relay read-back disagreed with the requested state on the previous tick (the controller then re-initialises the MCP23017 and tries again; two mismatches in a row keep the relay released and raise an alert)

Every forced release is logged with its reason and published.

## 7. Command line interface (`cli.py`)

A typer application in the style of mcp9600_logger: `heatpump <command>`, every command takes `--config PATH` (default `config.yaml`, then `/etc/heatpump/config.yaml`), output is plain text tables by default and `--json` where it makes sense so other tools can consume it. Read-only commands open the database read-only and work while the service is running (WAL mode makes that safe). Commands are added in the phase where their data first exists.

| Command | Phase | What it does |
|---|---|---|
| `run` | 9 | The service loop. Foreground, logs to stdout; this is what systemd runs. |
| `db init` | 1 | Create the schema (idempotent). |
| `db info` | 1 | Path, size, row counts per table, first/last timestamp per table. |
| `prices fetch` | 2 | Fetch today (and tomorrow if available) from Tibber and store. `--show` prints the result. |
| `prices show [--date]` | 2 | Quarter table in local time with energy/total; marks the current quarter and planned pauses once the planner exists. |
| `plan show` | 4 | Current plan: pauses in local time, expected saving, reason, created_at. |
| `plan now` | 4 | Run the planner on current data and print the plan **without** storing it or touching the controller. |
| `stove` | 5 | Latest stove reading, state, age of the reading. `--watch` tails the MQTT feed. |
| `weather` | 6 | Latest outdoor reading and its age; `--fetch` polls Open-Meteo once. |
| `status` | 9 | One screen: mode, relay requested/actual, guard reason, time in current state, stove, outdoor, current price, pause minutes used in the last 24 h (price/stove/total), next planned pause, next price fetch. `--json` for scripting. |
| `pauses [--since]` | 9 | The `pause_events` table: kind, start, end, duration, end reason. |
| `log [--since] [--tail N]` | 9 | The `control_log`, one line per tick. |
| `export <table> --from --to [--format csv\|json] [--out FILE]` | 9 | Export any table for a time range; stdout by default. This is the data path to the talk's plots. |
| `replay --date` | 11 | Run the planner over a stored day and print what would have happened. |
| `relay status` | 7 | Driver name and current relay state. |
| `relay set paused\|running` | 7 | Manual relay control for bench testing; **refuses while the service is running** (checks a lock file). Dry-run driver just prints. |
| `heating [--watch]` | 7 | Latest heating-circuit temperature; `--watch` samples live from the MCP9600. |
| `hat scan` | 7 | List the devices found on the I2C bus with their expected roles (0x22 MCP23017, 0x66 MCP9600). |
| `mqtt publish-state` | 10 | Publish the current state once, for debugging the broker side. |
| `service install\|status` | 11 | Write/update the systemd unit from a template and show `systemctl status`. |

Conventions: subcommands grouped with typer sub-apps (`db`, `prices`, `plan`, `relay`, `hat`, `service`); `rich` tables for human output; non-zero exit code when the thing asked for does not exist (no plan, no prices); no command other than `run` and `relay set` ever changes the relay.

## 8. Running as a service

`heatpump run` is a long-running foreground process managed by systemd on the Pi:

- `systemd/heatpump.service`: `Type=simple`, `ExecStart=/home/pi/heatpump/.venv/bin/heatpump run --config /etc/heatpump/config.yaml`, `Restart=always`, `RestartSec=10`, `EnvironmentFile=/etc/heatpump/env` (Tibber token, MQTT credentials, mode 0600), `After=network-online.target`, `WantedBy=multi-user.target`. Runs as user `pi`, not root; `pi` must be in the `i2c` group.
- Startup order inside `run`: release relay → load config → open database (create schema if missing) → write lock file → start MQTT (publish `availability: online`, last will `offline`) → fetch prices if needed → first plan → start tasks (tick, stove subscriber, heating sampler, weather poller, price scheduler, command listener).
- Shutdown on SIGTERM/SIGINT: cancel tasks, release relay, publish `offline`, remove lock file, close database. The relay is released in a `finally` so an exception on the way down cannot leave it closed.
- Logging to stdout in a journald-friendly single-line format; `journalctl -u heatpump -f` is the log viewer. Log level from config.
- The lock file (`/run/user/<uid>/heatpump.lock` or next to the database) is what `relay set` checks to refuse while the service runs.
- The database directory is created by the install step with the right owner; the service never needs root.

## 9. Phases

### Phase 0: Repo scaffold
- The repo is already `uv init`-ed with Python 3.13. Add dependencies with `uv add`: `pydantic`, `pyyaml`, `typer`, `rich`, `aiomqtt`, `pyTibber`, `tenacity`, `httpx`, `smbus2`; dev: `pytest`, `pytest-asyncio`, `ruff`. `smbus2` is pure Python and installs fine on the dev machine; it is only *opened* when a hardware driver is selected. Fill in the pyproject description.
- On the Pi, `uv python install 3.13` if the system Python is older (uv manages the interpreter, so the Pi's apt Python does not matter).
- Copy and adapt `agents.md` from `mcp9600_logger` (same conventions, same review-then-commit workflow). This plan is already at `docs/plan.md`.
- `config.py`: pydantic models for the YAML above, `load_settings(path)`, env-var secrets.
- `cli.py` skeleton: typer app with the sub-apps from §7 (`db`, `prices`, `plan`, `relay`, `hat`, `service`) and the global `--config` option; every command a stub that says "not implemented in this phase" so `--help` already shows the whole surface. Mirror the structure of the mcp9600_logger CLI.
- Logging setup (stdout, systemd-friendly), `__main__.py`.
- `README.md` with deploy steps: `rsync` to the Pi, `uv sync` there, `systemd/heatpump.service`.
- Confirm on the Pi: `i2cdetect -y 1` shows `0x22` and `0x66` (plus `0x50`, the HAT EEPROM), and `pi` is in the `i2c` group.
- **Done when:** `uv run heatpump --help` works locally and on the Pi; `pytest` runs (one config test).

### Phase 1: Models and database
- `models.py` dataclasses: `Quarter`, `Pause(kind, start, end)`, `Plan`, `StoveState` enum (`cold`, `burning`, `stale`), `ControlDecision`.
- `db.py`: `connect()`, `create_schema()`, upsert/select functions for every table in §4 (including `heating_readings`). Plain `sqlite3`, parameterised SQL, no ORM.
- CLI: `db init`, `db info`.
- Tests with an in-memory database: round-trip each table; `prices_for_range()` returns quarters in order and reports gaps.
- **Done when:** schema creates on the Pi at the configured path; tests pass.

### Phase 2: Tibber prices
- `prices.py`: `fetch_prices(token)` with pyTibber (`priceInfo`, `resolution: QUARTER_HOURLY`, today + tomorrow); map to `prices` rows. Study `fridge` for the client pattern but request quarters.
- `tenacity` retry: exponential backoff 1 min → 32 min, stop at `fetch_give_up_local_time`, retry only on network/API errors.
- `should_fetch(now_local, latest_slot_in_db)`: true at startup if today's quarters are missing, and after 14:00 if tomorrow's are missing.
- CLI: `prices fetch [--show]`, `prices show [--date]` (rich table in local time).
- Tests mock the Tibber response with a saved JSON fixture (96 quarters, plus a 92/100-quarter DST day).
- **Done when:** real prices from the Pi land in the database.

### Phase 3: Planner core
- Port `pick_pauses_with_hold` and `follows_pause_rules` from `heatpump_models.py` into `planner.py`, parameterised by `settings.price_pause`.
- `budget.py`: rolling-24 h paused minutes and "in recovery until" from a list of pause events.
- Unit tests with hand-made price days: one sharp peak, two peaks, a flat day, a day with the budget already half spent, a day where the last pause ended 10 minutes ago (recovery blocks).
- **Done when:** `plan_pauses()` passes the tests and never violates the rules on random price series (a property test with 200 random days).

### Phase 4: Planner scoring and horizon
- Net-saving scoring and the catch-up costing from §5 step 4; `min_expected_saving_sek`; horizon across midnight when tomorrow is known; stove pause as a blocked interval; keep-in-progress rule.
- CLI: `plan show` (stored plan) and `plan now` (run the planner on current data without storing).
- Export a few days from `tibber_prices.parquet` (2026-02-19, 2026-08-11, 2026-10-10, 2026-09-23) to CSV fixtures in `tests/fixtures/` and assert: 2026-08-11 produces no money-losing pause, 2026-10-10 produces an empty plan, 2026-02-19 pauses over the morning spike.
- **Done when:** `heatpump plan now` on the Pi shows a sensible plan for tomorrow.

### Phase 5: Stove monitor
- Check the payload format in `mcp9600_logger` (topic, JSON fields, availability values) and mirror it.
- `stove.py`: aiomqtt subscriber task; `StoveMonitor` with `update(temperature, ts)` and `state(now)` implementing the hysteresis (`burning` at ≥ 50, `cold` at ≤ 40) and staleness (`stale` after 300 s or availability offline). Stores every reading.
- CLI: `stove [--watch]`.
- Tests: hysteresis sequence, stale → cold handling, out-of-order timestamps ignored.
- **Done when:** `heatpump stove --watch` shows live stove temperature and state on the Pi.

### Phase 6: Outdoor temperature
- `weather.py`: Open-Meteo `GET /v1/forecast?latitude&longitude&current=temperature_2m&timezone=UTC` with httpx, every 30 min, stored in `outdoor_readings`; stale after 3 h is only informational in this phase.
- Keep a `WeatherProvider` protocol so Trafikverket or the developer's own sensor via MQTT can replace it later.
- Optional, if time allows: `max_minutes` for price pauses interpolated from a small `(outdoor °C → minutes)` table in config, default off.
- CLI: `weather [--fetch]`.
- **Done when:** `heatpump weather --fetch` prints the outdoor temperature on the Pi.

### Phase 7: HAT drivers (relay and heating-circuit thermocouple)
- `i2c.py`: open the bus once (`smbus2.SMBus`), `read_register` / `write_register` helpers, a lock so the relay and the MCP9600 never interleave a transaction.
- `mcp23017.py`: configure GPB0 as output with everything else untouched (read-modify-write of IODIRB), `set_pin(high)` writes OLATB, `read_pin()` reads GPIOB. Document the register map at the top of the file.
- `mcp9600.py`: port the driver from `mcp9600_logger` (same chip, same HAT); hot-junction temperature, cold-junction, status/open-circuit flag.
- `relay.py`: `Relay` protocol (`set(paused: bool)`, `read() -> bool`, `release()`), `DryRunRelay` (in-memory, logs every change) and `Mcp23017Relay` (write, then read back; raise `RelayMismatch` if they differ). `release()` is what startup and shutdown call.
- `heating.py`: `HeatingSampler` task, one reading per `sample_interval_seconds` into `heating_readings`; an open-circuit flag is logged, not stored as a temperature.
- CLI: `hat scan`, `relay status`, `relay set paused|running` (refuses while the lock file exists), `heating [--watch]`.
- Tests with a fake bus object: IODIR is only changed on the one pin; set/read-back mismatch raises; MCP9600 decoding against the fixture values from mcp9600_logger's tests.
- **Bench test on the Pi:** `relay set paused` clicks the relay, `relay set running` releases it, `heating --watch` tracks a hand on the thermocouple. The relay is not connected to EXT, so this is harmless.
- **Done when:** the bench test passes and tests pass on the dev machine without hardware.

### Phase 8: Safety guard
- `guard.py` exactly as in §6, pure, with a `GuardState` input (paused since, paused minutes in 24 h, price present, stove state, clock sanity, force_run, last read-back ok).
- The read-back rule: one mismatch → release, re-initialise the MCP23017 and retry next tick; two in a row → stay released and publish an alert.
- Tests for every guard rule, including "guard can never turn False into True" as a property test over random states.
- **Done when:** tests pass.

### Phase 9: Controller loop
- `controller.py`: one tick per minute. Reads plan, stove state, guard inputs; computes `mode` ∈ {`running`, `paused_price`, `paused_stove`, `recovery`, `forced_run`}; opens/closes `pause_events`; writes `control_log`; calls the relay; triggers re-plans per §5. Stove pause cap and recovery handled here using `budget.py`.
- A `ControllerState` dataclass that survives restarts by reconstructing from `pause_events` (so a restart mid-pause doesn't reset the clock).
- Startup and shutdown sequence as in §8, including the lock file and the relay release in `finally`.
- CLI: `run`, `status [--json]`, `pauses`, `log`, `export`.
- Tests drive the tick with a fake clock, the `DryRunRelay`, and scripted stove/price inputs: stove lit before a planned peak re-plans and moves the budget; 6 h stove cap releases and recovery follows; restart mid-pause continues the pause; a read-back mismatch releases.
- **Done when:** `heatpump run` runs for an hour on the Pi with the real relay (still not connected to EXT) and the control log shows the relay clicking in and out as planned.

### Phase 10: MQTT publishing and commands
Topics under `heatpump/` (retained unless noted):

| Topic | Payload |
|---|---|
| `availability` | `online` / `offline` (last will) |
| `state` | JSON: mode, relay, since, guard_reason, stove_state, stove_temp, outdoor_temp |
| `relay` | `paused` / `running` |
| `plan` | JSON: created_at, expected_saving_sek, list of pauses (local and UTC times) |
| `price/current` | JSON: slot_start, energy, total |
| `price/today`, `price/tomorrow` | JSON arrays of quarters |
| `heating/temperature` | Heating-circuit temperature, every sample |
| `alert` (not retained) | JSON: reason, ts; published on guard releases, relay mismatches, stale feeds |
| `cmd/force_run` (subscribe) | `on` / `off`, latched in the guard |
| `cmd/replan` (subscribe, not retained) | any payload |

Publish `state` every tick and on change; `plan` after every planner run; prices after every fetch.
- CLI: `mqtt publish-state`.
- **Done when:** topics are visible from the broker with `mosquitto_sub`.

### Phase 11: Service and soak
- `systemd/heatpump.service` as in §8, plus `/etc/heatpump/config.yaml` and `/etc/heatpump/env` layout; `service install` renders the unit, `service status` wraps systemctl. Install instructions in README.
- CLI: `replay --date YYYY-MM-DD`: runs the planner on stored prices for a past day and prints what would have happened; the start of the simulator for the talk.
- Run for several days with the relay clicking but not connected to EXT; review `control_log`, `pause_events` and the heating-circuit curve with the developer before phase 12.
- **Done when:** the service survives a reboot and a broker outage, the relay is always released after a restart, and the log shows the stove pauses lining up with real fires.

### Phase 12: Connect EXT and commission (the developer, with the agent on standby)
- Stop the service, confirm `relay status` says running, wire the relay contact to the Rego EXT terminals, set menu 5.7 = 1.
- `relay set paused`: the Rego display should show the EXT block and the compressor should stop; `relay set running` releases it. Note the compressor restart delay.
- Start the service; watch the first planned pause and the first stove pause on the Rego display and in `heating --watch` (the circuit temperature should start falling during the pause).
- Set a conservative `absolute_max_pause_minutes` (e.g. 180) for the first week, then relax to 360.
- **Done when:** a planned pause and a `cmd/force_run` release are both visible on the Rego, and the heating-circuit curve during the pause looks as expected.

### Later, not planned here
- Use the heating-circuit temperature as a comfort guard: end a pause early if the circuit drops below a threshold.
- Indoor probes via MQTT as a comfort guard and a replacement for the time caps.
- Rego service port: pre-heat via fine adjustment (model 4), hot water target scheduling, GT2 instead of Open-Meteo.
- Trafikverket road-weather provider.

## 10. Open items the agent should not decide alone
- Exact MQTT payload of `stove/temperature` (read it from the mcp9600_logger code).
- Whether `/var/lib/heatpump` or a home directory is preferred for the database on the Pi.
- Lat/lon of the house (the config example holds an approximation).
- Anything that would widen the relay's reach: new reasons to pause, longer caps, or guard rules removed.
- Connecting the relay to EXT (phase 12) is done by the developer by hand, never by the agent.
