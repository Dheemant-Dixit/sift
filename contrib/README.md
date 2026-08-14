# Running sift in the background

`sift find` and `sift ask` sync the index before every query, and a sync where
nothing changed takes well under a second — so for most people **none of this is
necessary**.

It becomes worth it when the first sync after a busy day is slow enough to
notice: the work happens in the background instead of while you're waiting for
an answer.

sift does **not** install any of this for you. Copy the file, edit the paths,
and load it yourself.

---

## macOS (launchd)

Two approaches. Pick one.

**`com.sift.watch.plist`** — a resident process watching the folder. Re-indexes
seconds after a download lands. Costs you an idle Python process.

**`com.sift.sync.plist`** — no resident process. launchd itself watches the
folder and runs a one-shot `sift index` when it changes.

Both need real paths. Find yours with:

```bash
which sift          # -> put this in <string> under ProgramArguments
echo "$HOME/Downloads"
```

Then:

```bash
cp com.sift.sync.plist ~/Library/LaunchAgents/
# edit it: replace /PATH/TO/sift and /PATH/TO/Downloads
launchctl load ~/Library/LaunchAgents/com.sift.sync.plist

# check it:
launchctl list | grep sift
tail -f /tmp/sift.log

# stop it:
launchctl unload ~/Library/LaunchAgents/com.sift.sync.plist
```

---

## Linux (systemd user units)

**`sift-watch.service`** — the resident watcher, equivalent to the plist above.

```bash
mkdir -p ~/.config/systemd/user
cp sift-watch.service ~/.config/systemd/user/
# edit it: replace /PATH/TO/sift
systemctl --user daemon-reload
systemctl --user enable --now sift-watch.service

journalctl --user -u sift-watch -f
```

**`sift-sync.service` + `sift-sync.timer`** — a periodic one-shot sync instead,
if you'd rather not keep a process alive.

```bash
cp sift-sync.service sift-sync.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sift-sync.timer
```

---

## Windows

No unit files here. Use Task Scheduler:

1. Create Task → Triggers → *On a schedule*, repeat every 15 minutes.
2. Actions → Start a program → the full path to `sift.exe`, arguments `index`.
3. Under Conditions, clear *Start only if on AC power* if you want it on battery.

A folder-change trigger is possible via Event Viewer subscriptions but is more
trouble than the timer is worth.
