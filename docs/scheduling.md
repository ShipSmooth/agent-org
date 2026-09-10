# Scheduling: making the Monday report arrive on its own

The weekly report is read-only and always has been, so it is safe to run
unattended. Filling a supplier's cart is not, and never becomes
automatic: `shannon stage --live` is typed by a person, the week they
mean it. This page is about the first thing only.

## The three pieces

1. **The schedule**, in `config/entities/<entity>.yaml`:

   ```yaml
   agents:
     - name: shannon
       kind: shannon_replenishment
       schedule: "cron: 0 6 * * MON"
   ```

   Read in the entity's own timezone, the one named in the same file —
   `0 6 * * MON` means six in the morning where the business is, not six
   UTC.

2. **`shannon tick`**, which reads that schedule, and runs this week's
   replenishment if it is due and has not run yet. If nothing is due it
   prints so and exits 0. It reads the live Veeqo account and inbox
   unless told otherwise, because a timed run that quietly reported
   last month's saved exports would be worse than no report at all.

3. **A timer**, which calls `shannon tick` every hour.

Hourly, rather than a timer set to Monday 06:00, because the machine is
not always on at 06:00 on a Monday. "Due" lasts the rest of the week, and
the run happens once per ISO week, so a Monday spent switched off is
picked up on the Tuesday and never runs twice. Nothing runs while the
machine is off; there is no catch-up from a cloud anywhere.

## What the timer cannot do

`shannon tick` can start exactly one thing: the replenishment report. It
has no path to a cart:

- `cmd_tick` calls `_run_the_week`, which calls `run_replenishment` and
  `deliver_report` — no cart executor is reachable from either.
- Staging is a separate subcommand (`shannon stage`), needing `--live`,
  and gated again by the tier ceiling in `config/policy/global.yaml`.
- `tests/test_tick_command.py` asserts this rather than trusting it: the
  test fails if `tick` ever reaches `stage_supplier_cart` or any cart
  client.

There is also no flag on `tick` that turns it into a staging run. Adding
one would be the wrong shape of change; if staging ever needs a schedule,
it needs an approval step first, not a timer.

## Installing it (Linux, systemd)

Unit files are in `deploy/systemd/`. They assume the checkout is at
`/opt/agent-org`, owned by a `shannon` user, with credentials in
`/opt/agent-org/.env` (see docs/live-data.md). Edit the paths and the
user in the service file if yours differ, then:

```bash
sudo cp deploy/systemd/shannon-tick.service /etc/systemd/system/
sudo cp deploy/systemd/shannon-tick.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shannon-tick.timer
```

Check it:

```bash
systemctl list-timers shannon-tick.timer   # when it next fires
systemctl status shannon-tick.service      # how the last run ended
journalctl -u shannon-tick.service -n 50   # what it said
```

Run it once by hand, without waiting for the hour:

```bash
sudo systemctl start shannon-tick.service
```

To stop it firing at all — during a holiday, or while the parts list is
being reworked:

```bash
sudo systemctl disable --now shannon-tick.timer
```

## Installing it on the Dell, which runs Windows

The Dell is a Windows machine running WSL2, so systemd is available only
inside the WSL distribution — and only while that distribution is
running. WSL shuts its distributions down when nothing is using them,
and a stopped distribution runs no timers, so the units above installed
straight into WSL will quietly stop firing. Two ways round it, in order
of preference:

1. **Let Windows do the timing.** Task Scheduler is always running, and
   the command it calls is the same one:

   ```powershell
   schtasks /create /tn "Shannon tick" /sc hourly /ru Zach /rl LIMITED ^
     /tr "wsl.exe -d Ubuntu -- bash -lc 'cd /opt/agent-org && uv run shannon tick'"
   ```

   Tick "Run task as soon as possible after a scheduled start is
   missed" in the task's Settings tab — that is `Persistent=true`.
   Starting the task starts WSL, so nothing needs to be kept alive.

2. **Keep the distribution up and use systemd inside it**, with
   `systemd=true` under `[boot]` in `/etc/wsl.conf` and something
   holding the distribution open across logins
   (`wsl.exe --manage <distro> --set-sparse` does not do this; a
   Windows-side scheduled `wsl.exe -d <distro> true` at boot does).
   This is more moving parts for the same outcome.

Either way `shannon tick` behaves identically: it is the schedule, not
the timer, that decides whether a run happens.

## Reading the result

Either way the run writes its report to the `reports` folder and emails
it to the owner address in `config/ithrive/shannon.yaml`. An hour where
nothing was due leaves a one-line journal entry and no email. A run that
fails leaves a failed unit and a non-zero exit, and sends nothing — a
missing email is the signal to look, which is why `tick` does not
swallow errors.
