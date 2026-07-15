# Lexgraph refresh jobs

Production has two complementary refresh paths:

- `refresh-server.sh` runs the complete daily corpus/history pipeline.
- `refresh-procedure-watch.sh` runs only the live DIP procedure snapshot, the
  explicitly configured EUR-Lex watches, persistent watch state/history and
  the web-data build.  `lexgraph-procedure-watch.timer` schedules this path at
  08:17 and 20:17 UTC, with up to five minutes of randomized delay.

Both server scripts acquire `/run/lock/lexgraph-refresh.lock`.  The workstation
`refresh-and-push.sh` uploads into a unique incoming directory and takes that
same **remote** lock before publishing it.  If a delayed timer overlaps the
other refresh, the later job exits successfully without touching snapshots or
the published data.  This prevents concurrent `build_web_data.py`/publish runs
on the small production host.  A subsequent scheduled run performs the next
observation.

Publishing never writes into the live API directory.  `publish-web-data.sh`
first copies into a versioned release, parses the required JSON, runs SQLite
`quick_check`, verifies act files, then atomically switches the `web-data`
symlink and restarts the API.  Three complete generations are retained for
rollback.  The first run migrates the legacy mutable directory while the API
is briefly stopped.

The lifecycle is intentionally evidence-based.  An unchanged check updates
`last_checked` but does not append a duplicate history event.  A terminal
record remains in `data/procedure_watch_state.json` and
`data/procedure_watch_history.jsonl` as an archive; the EUR-Lex fetcher then
excludes it from later network polls.  EU political agreement alone is not a
terminal event: the watcher waits for the adopted CELEX and Official Journal
publication evidence, then remains `pending_final_review` until a persisted
review has compared the final Article 2 with the tracked Commission proposal.

## Install/update the production timer

Run as root on the production host after the repository is synchronized:

```bash
install -m 0755 deploy/refresh-procedure-watch.sh \
  /srv/sntiq-lexgraph/deploy/refresh-procedure-watch.sh
install -m 0755 deploy/publish-web-data.sh \
  /srv/sntiq-lexgraph/deploy/publish-web-data.sh
install -m 0644 deploy/lexgraph-procedure-watch.service \
  /etc/systemd/system/lexgraph-procedure-watch.service
install -m 0644 deploy/lexgraph-procedure-watch.timer \
  /etc/systemd/system/lexgraph-procedure-watch.timer
systemctl daemon-reload
systemctl enable --now lexgraph-procedure-watch.timer
systemctl list-timers lexgraph-procedure-watch.timer
```

Trigger and inspect one run without waiting for the timer:

```bash
systemctl start lexgraph-procedure-watch.service
systemctl status lexgraph-procedure-watch.service
journalctl -u lexgraph-procedure-watch.service -n 100 --no-pager
```

The service is resource-controlled (`Nice`, CPU/IO weights and a 420 MiB hard
memory limit) so a source or build regression cannot consume the entire 1 GiB
VPS.  `EnvironmentFile=-/etc/sntiq/lexgraph.env` remains optional.
