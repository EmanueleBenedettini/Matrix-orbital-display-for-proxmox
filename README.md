# Proxmox status on a character display

Connects to LCDd.

<img width="1280" height="960" alt="5773936205346050570" src="https://github.com/user-attachments/assets/ba29fb2c-9625-4244-9a4e-f7690f8ed3ee" />

Five rotating screens on the 20x4 display attached to a Proxmox VE host: node name
and uptime and load, CPU and RAM, VM/container counts, storage usage (scrollable when
there are more volumes than rows), and the network interface this node routes through
with its address and live bandwidth.

## The files

| file | what it is |
|---|---|
| `pve-lcd.py` | the client. Reads Proxmox, sends text to LCDd. Needs no extra packages (Python 3 standard library only). |
| `pve-lcd.service` | systemd unit that keeps the client running and restarts it. |
| `install.sh` | patches `/etc/LCDd.conf`, installs the client and the unit, and offers the diagnostics commands. |
| `/etc/systemd/system/pve-lcd.service.d/20-buttons.conf` | written by `install.sh keys-*`: the key names the client reserves, and who rotates the screens. It overrides the unit, so it survives the next `install.sh install`. |

## Install

    bash install.sh status              # what is running now, no changes
    bash install.sh fix-conf --dry-run  # shows the exact config diff, changes nothing
    bash install.sh install             # patch config, install client + unit, start it
    bash install.sh keys-default          # map the six buttons and tell the client their names
    bash install.sh restart-lcd         # reload LCDd so the new config is actually used

`install.sh install` writes a backup first (`/etc/LCDd.conf.bak.<date-time>`) and
prints the diff before it writes anything. `restart-lcd` stops every LCDd process it
finds and starts the server again - that step is what clears the stale server that
was holding the old settings. `keys-default` writes both halves of the button setup at once
(`KeyMap_` lines in `/etc/LCDd.conf`, the matching `[menu]` lines, and the key names the
client reserves) and restarts the client, so it is safe to run it last; whichever `keys-*`
command you run last is the mapping you get.

Check it worked:

    bash install.sh status
    journalctl -u pve-lcd.service -n 40 --no-pager

The log should contain `display is <W>x<H>`, `registered 5 screen(s)` and
`buttons this client handles:`.

## What the five screens show

Each screen is exactly 4 lines of 20 characters. The client moves the panel from one
to the next every 10 seconds, starting as soon as it connects — no button press needed
at boot. ("Buttons" below explains what the keys do, and how to hand the rotation back
to LCDd instead.)

| screen | on screen | duration |
|---|---|---|
| `node` | node name, uptime, 1/5/15 minute load, node count and quorum, clock | 10 s |
| `res` | CPU percent and core count with a bar, RAM used/total and percent with a bar | 10 s |
| `guests` | VMs running/total, containers running/total, and the vmids that are not running | 10 s |
| `disks` | an overview line (how many volumes, how many over 90%), then the fullest volumes with their used percent; with two or fewer volumes each one gets a full-width bar; with more volumes than rows the list is a window you can move with the up/down arrows | 10 s |
| `net` | the interface this node routes through with its link state (and negotiated speed, when the row has room), its IPv4 address with prefix, the gateway, live rx/tx bandwidth; the MTU is printed instead of a row when the panel is taller than four rows | 10 s |

Values come from `pvesh` where available, and from `/etc/pve/...`, `/proc/stat`,
`/proc/meminfo`, `/proc/loadavg`, `/proc/uptime`, `/proc/net/dev`, `/proc/net/route`,
`/sys/class/net/...` otherwise. Everything except the `/proc` and `/sys` numbers is
polled on a timer (30 s for the guest lists, 180 s for storage, 60 s for the cluster
check and 60 s for the interface details), never on every repaint — see "Why the panel
costs CPU" below for the numbers and for how to make them cheaper.

On the storage screen the fullest volumes come first. `pvesh` reports `total`, 
`avail` and `used_fraction` for each volume, and the
client trusts `used_fraction` when it is present.
Volumes reported as disabled are skipped.

The network screen picks the interface the node's default route uses (the one a packet
leaves through), so what it names is the interface you would actually troubleshoot; with
no default route it falls back to the interface that has moved the most traffic. Its
bandwidth is measured from two `/proc/net/dev` counter samples taken on the ordinary
2 second refresh, which is what makes it free: like CPU, RAM, uptime and load, it never
runs `pvesh` or any other program.

The address and the gateway each get a row of their own, and that is a width decision
rather than a style one: `192.168.1.42/24` is 15 columns and `gw 192.168.1.1` is 14, so
two dotted IPv4 addresses side by side do not fit a 17 or 20 column panel, and forcing
them onto one row produces a chopped line such as `no a>172.31.208.1`. Each one gets the
full width instead, and the wording gives way before the characters do: the address row
reads `ip 192.168.1.42/24` at 20 columns, `192.168.1.42/24` at 17, and `192.168.1.42` at
16. Link state and the negotiated speed share the first row (`vmbr0   1G up`), and the
MTU is only printed on a panel taller than four rows — on a four row panel the live rx/tx
numbers matter more than a setting that never changes.

## How many columns the panel really has

LCDd never measures the panel. The driver writes exactly as many columns as `Size=` in
`[MtxOrb]` claims, and when that number is bigger than the panel, the columns past the last
real one are written but never shown: text shifts, words are cut, a bar ends in mid-air.
If your eye counts 17, the panel is 17 columns wide, whatever the label or the datasheet says.

Check it on the panel instead of guessing:

    bash install.sh measure

It stops `pve-lcd.service` so nothing repaints, paints a ruler, and starts the service again
afterwards. The top row is numbered `12345678901234567890` and one row below it is the
letters `A` onward, one character per column. Read the last character that is actually
visible on the panel: a number row ending in `7` means 17 columns, and a last letter of `Q`
means 17 too. Then write what you saw:

    bash install.sh size 17x4

The client reads the size out of LCDd's own opening banner every time it connects and logs
it — `journalctl -u pve-lcd.service | grep 'display is'` — and lays every screen out for
that width, so once `Size` is right the text fits without changing anything else. Later
`install.sh install`, `fix-conf` and every `keys-*` command keep the Size you measured; only
`install.sh size` changes it.

## Buttons

If your panel has keys, you can map them.

**Use the arrows to drive the screens** — left/right pick which screen is on the panel,
up/down move through a list that has more entries than the panel has rows — and keep the X
key for LCDd's own menu:

    bash install.sh keys-default --dry-run   # shows the diff, changes nothing
    bash install.sh keys-default             # C=Up H=Down D=Left E=Right I=Enter J=Escape
    bash install.sh restart-lcd

back arrow = previous screen, forward arrow = next screen, up/down = scroll the storage
list when it has more volumes than rows, tick = hold this screen on the panel / let the
screens rotate again, X = open and close LCDd's menu (Options: Heartbeat, Backlight,
TitleSpeed; MtxOrb: Contrast, On Brightness, Off Brightness).

Each press answers on the panel: the corner of the last row briefly reads `| Prev` or
`| Next` after an arrow, `| Hold` or `| Rotate` after the tick — the same words, in the
same corner, that LCDd used to write for its own screen keys.

`bash install.sh keys-nav` is the same mapping with the X key left out: no `KeyMap_J`
line, and any `[menu]` key lines commented out, so no button can open a menu and the panel
is driven only by the client's screens. Use it when you would rather X did nothing.

Whichever of these commands you run, it also writes
`/etc/systemd/system/pve-lcd.service.d/20-buttons.conf`, which tells the client the key
*names* it should ask LCDd for. The two halves have to agree: a `KeyMap_D=Left` line is
worthless if the client never asked for `Left`, and asking for a name the panel cannot
produce reserves a key that never arrives.

**The same mapping with your own letters written out:**

    bash install.sh keys-menu C=Up H=Down D=Left E=Right I=Enter J=Escape
    bash install.sh restart-lcd

back/forward change the screen, up/down scroll the storage list while a status screen is
showing and walk the menu while the menu is showing, tick selects a value, X opens and
closes the menu. Inside it: Options (Heartbeat, Backlight, TitleSpeed) and MtxOrb (Contrast,
On Brightness, Off Brightness). Scrolling the storage list is done by this client, not by
LCDd: 0.5.9 has the `ScrollUpKey`/`ScrollDownKey` settings but their handlers are empty,
so nothing else could scroll anything.

The menu is not a place you get stuck. An open menu outranks every client screen, so it
takes the panel over, and closing it puts the pinned status screen straight back.
`Escape` is the one name a client can never reserve for itself — LCDd's menu holds it
exclusively — which is why mapping X to `Escape` keeps the menu reachable no matter what
the client asks for.

There is a reason that is a command of its own. The menu is driven by the `[menu]` section,
and every value there must be a key *name the driver really produces* — one of the names on
the right hand side of a `KeyMap_` line. A config that only has `MenuKey=Escape` opens the
menu and then nothing inside it can move: that is exactly the "I can open it, but the buttons
do nothing in it" symptom. `keys-menu` writes both halves from one mapping — the `KeyMap_`
lines and the matching `[menu]` lines — so they cannot disagree. `keys-nav` does the opposite
and comments the `[menu]` keys out, so no button can open a menu the buttons cannot walk.
`bash install.sh status` prints both sections so you can see which one you have.

Two buttons mapped to the same name cannot be told apart by LCDd: it only ever receives the
name, never the character the key sent. Both default mappings give the four arrows four
different names for that reason; if you write your own and give two arrows the same name,
they become two copies of one action — which is only useful when both of them do the thing
you want repeated.

If a button does not react, read the characters it actually sends:

    bash install.sh keys-probe
    kill $(pgrep -f '/usr/sbin/LCDd'); sleep 2
    /usr/sbin/LCDd -f -c /etc/LCDd.conf   # press each button: it prints "Received character X"
    # Ctrl-C to stop it, then map the letters you saw:
    bash install.sh keys-default C=Up H=Down D=Left E=Right I=Enter J=Escape
    bash install.sh restart-lcd

Only characters A-Y can be mapped at all; anything else the driver reports as
"Untreated key". And `keypad_test_mode` must be `no` for a mapping to work — while it is
`yes` the server only prints the character and ignores it. Both key commands set it back
to `no` for you.

## Who changes the screens, and why it is the client

By default the client does it, and that is deliberate. LCDd only advances to the next
screen when `[server] AutoRotate` is switched on, and no command a client is allowed to
send switches it on: a client that wants the panel to start changing pages at boot has
to change them itself.

So the client keeps exactly one of its screens pinned at `-priority foreground` and the
others at `info`. LCDd sorts its screen list by priority every frame and snaps to the
highest-ranked screen, which is the pinned one; and its rotation only ever walks inside a
single priority class, so with one foreground screen there is nothing for it to walk to.
When the client wants the next page it moves the pin, and the panel changes within a
fraction of a second. `AutoRotate=yes` is still written into the config, because LCDd
should be allowed to rotate for anyone else who connects.

That makes the start of a boot deterministic: the panel opens on the node screen — the
first in the list — and turns on its own, 10 seconds per screen, about 50 seconds for a
full turn of the five. A value you changed can take that long to come up if you touch
nothing. Pressing an arrow jumps ahead and the rotation continues from there; the tick
holds a screen on the panel until you press it again. To keep the node page up longer on
its own, raise only its number (for example `80` for the others and `160` for `node`).

If you would rather LCDd did the rotating, write `PVELCD_ROTATION=lcdproc` in a drop-in.
Then every screen of ours stays at `info`, `AutoRotate=yes` and the `-duration` values
decide what shows and for how long, and LCDd's own `PrevScreenKey`, `NextScreenKey` and
`ToggleRotateKey` keep working because in that mode the client only reserves Up/Down (the
two keys 0.5.9 does nothing with by itself). Up/Down then scroll every list that has more
rows than fit, since the client cannot know which screen the panel happens to be showing.

### The word in the corner

0.5.9 writes a short word in the bottom-right corner whenever a *screen* key changes what
you see: `input.c:155-170` calls `server_msg()` with `Hold` when `ToggleRotateKey` stops
the rotation, `Rotate` when it starts it again, `Prev` and `Next` for the two arrows, and
`render.c:180-186` paints `| Hold` and friends over the end of the last row for four
frames. Its `scroll_up_key` and `scroll_down_key` branches are empty, which is why
scrolling never showed a word.

Those handlers only run for keys LCDd still handles. Because the client reserves Enter and
both arrows in `client` mode, LCDd's corner word disappeared along with them, and no
client command can make LCDd print one. So the client writes the same four words in the
same place, over the end of the bottom row of the screen that is actually on the panel:

    |17:44:11 > | Hold|

The row keeps what fits in front of the word, and the `>` marks what the word covered. It
stays for `PVELCD_NOTICE_SECONDS` seconds (2.5 by default) and then the row comes back on
its own; `PVELCD_NOTICE_SECONDS=0` never writes it. In `PVELCD_ROTATION=lcdproc` mode LCDd
still writes its own word, so the client does not write one there.

## Changing what you see

Open `pve-lcd.py` and edit the values at the top, then
`bash install.sh install` again (or just copy the file to `/usr/local/bin/pve-lcd.py`
and `systemctl restart pve-lcd`).

    DURATIONS = {"node": 80, "res": 80, "guests": 80, "disks": 80, "net": 80}   # eighths of a second: 80 = 10 s
    WARN_FILL = 90.0        # percent that counts as "almost full"
    SCREENS = {...}         # which screens exist and in what order
    KEY_ACTIONS = {...}     # what each reserved button does

Only the screens you want, and a different refresh rate, without editing the file:

    /usr/local/bin/pve-lcd.py --screens node,res,net --interval 3
    /usr/local/bin/pve-lcd.py --test              # print all five panels to the terminal
    /usr/local/bin/pve-lcd.py --verbose           # also logs key presses and every reply

To make that permanent, edit `ExecStart=` in `/etc/systemd/system/pve-lcd.service`
and run `systemctl daemon-reload && systemctl restart pve-lcd`. Environment variables
work too, and a drop-in keeps them across the next `install.sh install`:

| variable | effect |
|---|---|
| `PVELCD_HOST`, `PVELCD_PORT`, `PVELCD_REFRESH`, `PVELCD_VERBOSE` | where to connect, how often to repaint, extra logging |
| `PVELCD_KEYS` | the key names this client asks LCDd for; they must match the names on the right of your `KeyMap_` lines. Empty means the client ignores the buttons. `install.sh keys-*` writes it for you. |
| `PVELCD_ROTATION` | `client` (default) rotates the screens itself; `lcdproc` leaves it to LCDd and its own screen keys |
| `PVELCD_NOTICE_SECONDS` | how long the corner word (`\| Hold`, `\| Rotate`, `\| Prev`, `\| Next`) stays after a button press; 0 never writes it (default 2.5) |
| `PVELCD_NET_TTL` | how often the interface details are re-read (60 s) |
| `PVELCD_NET_IFACE` | force one interface instead of the one the default route uses, e.g. `vmbr0` |
| `PVELCD_GUEST_TTL`, `PVELCD_STORAGE_TTL`, `PVELCD_CLUSTER_TTL`, `PVELCD_GUEST_SOURCE`, `PVELCD_SLOW_PVESH`, `PVELCD_RETRY_TTL` | the polling rates, in the next section |

## Why the panel costs CPU, and how to make it cheap

The panel itself costs almost nothing: the client repaints every 2 seconds by writing
strings to a socket, and CPU percent, RAM, uptime, load, the cluster check and the whole
network screen come from `/proc`, `/sys` and `/etc/pve`. What does cost CPU is `pvesh`. Every `pvesh get ...` is a Perl
process that starts up, loads the whole Proxmox API and only then answers — typically a
second or more of real CPU, with a few hundred MB of peak memory. Those processes are
children of the client, so **systemd charges their CPU to `pve-lcd.service`**: that is how
a client that paints four rows logs hours of CPU time in the journal.

So the client polls, it does not query on every repaint:

| data | source | polled every | `pvesh` calls |
|---|---|---|---|
| VM and container lists | `pvesh get /nodes/<node>/qemu` and `/lxc` | 30 s | 2 |
| storage usage | `pvesh get /nodes/<node>/storage` | 180 s | 1 |
| node count and quorum | `/etc/pve/nodes`, `/etc/pve/corosync.conf` | 60 s | none |
| interface address, gateway, link speed, MTU | `/proc/net/route`, one socket ioctl, `/sys/class/net/<iface>` | 60 s | none |
| rx/tx bandwidth | `/proc/net/dev`, measured between two samples | every 2 s | none |
| CPU %, RAM, uptime, load | `/proc` | every 2 s | none |

A little over 4 `pvesh` calls a minute. Measure before blaming anything:

    bash install.sh cpu-diag

It times the CPU of each unit over 40 seconds, lists every `pvesh` process that appeared
in that window together with the process that started it (so you can see whether it is
this client, the web interface or another monitor), prints the polling settings the units
are running with, and prints what the client logged about the duration of its own calls.
It ends with the clean separation test: stop the client, run the check again, and any
`pvesh` that still appears belongs to something else.

To change the rates, override them in the unit — a drop-in override beats the installed
defaults and survives the next `install.sh install`:

    mkdir -p /etc/systemd/system/pve-lcd.service.d
    printf '[Service]\nEnvironment=PVELCD_GUEST_TTL=90\nEnvironment=PVELCD_STORAGE_TTL=600\nEnvironment=PVELCD_GUEST_SOURCE=files\n' > /etc/systemd/system/pve-lcd.service.d/less-cpu.conf
    systemctl daemon-reload && systemctl restart pve-lcd.service

(`systemctl edit pve-lcd.service` opens exactly that file, if you prefer an editor.)

`PVELCD_GUEST_SOURCE=files` drops the two guest `pvesh` calls entirely and reads the list
from `/etc/pve/qemu-server` and `/etc/pve/lxc`. The tradeoff is that the `status:` line in
those files is what `pvedaemon` last recorded, so a VM that died without Proxmox noticing
can still read `running`.

`journalctl -u pve-lcd.service | grep pvesh` prints one line per call slower than
`PVELCD_SLOW_PVESH` (default 1.5 s; set it to `0` to log every call), which turns "is this
the reason?" into measured seconds. `PVELCD_REFRESH` (2 s) is not a `pvesh` knob — raising
it only makes the panel update later. And a collector that fails is retried no faster than
every `PVELCD_RETRY_TTL` seconds (15), so a broken source cannot turn into a call every 2
seconds.

## If something is wrong

| symptom | what to do |
|---|---|
| panel shows `Clients: 0` / `Screens: 0` again | that is LCDd's own page: no client connected. `bash install.sh logs` and read the `pve-lcd` lines. |
| log says `LCDd reports 18x4, not 20x4` | `Size` in `[MtxOrb]` is wrong, or you did not run `restart-lcd` after patching. |
| log says `link problem ... Connection refused - retrying` | the display server is not running at all, so the client has nothing to talk to. `bash install.sh status` shows the state of the server service; `bash install.sh server-diag` says why (see the section below). |
| text is there but characters look wrong or the panel is dark | try `bash install.sh install --backlight-no` (some panels report an adjustable backlight they do not have), and check `Contrast`. |
| two LCDd processes listed by `install.sh status` | only one may own the serial port. `bash install.sh restart-lcd` cleans it up. |
| nothing on the panel, log shows the port is busy | another program holds `/dev/ttyUSB0`. `install.sh status` lists every LCDd it can find. |
| `no guest data` / `pvesh failed` on the panel | the client could not reach `pvesh`; it still shows CPU, RAM, uptime and load. `journalctl -u pve-lcd.service` says why. |
| `top` shows `/usr/bin/pvesh get ...` nearly all the time, or CPU is high | the client's data polling is the likely candidate, and its child processes' CPU is charged to `pve-lcd.service`. `bash install.sh cpu-diag` measures the CPU per unit, says who started each `pvesh` that appeared, and gives the commands that lower the polling rates. |
| log says `rejected by LCDd: huh? Wrong number of arguments` | `/usr/local/bin/pve-lcd.py` is an older copy than the one in this folder. Copy the folder again and `bash install.sh install`, then `systemctl restart pve-lcd`. The log line names the command LCDd refused. |
| text runs off the right edge, words are cut or the layout looks shifted | the panel has fewer columns than `Size` says. `bash install.sh measure`, read the ruler, then `bash install.sh size 17x4`. |
| X opens a menu but no button moves inside it | the `[menu]` section only carries `MenuKey`; the menu also needs `UpKey`/`DownKey`/`EnterKey` and they must name keys your panel can send. `bash install.sh keys-menu C=Up H=Down D=Left E=Right I=Enter J=Escape`, then `restart-lcd`. |
| buttons do nothing | MtxOrb does not read the keypad at all unless at least one `KeyMap_` line exists, and it ignores every key while `keypad_test_mode=yes`. `bash install.sh status` prints both; then `bash install.sh restart-lcd`. |
| log says `Untreated key 0xNN` | that key sends a character the driver cannot map (only A-Y work), or you mapped a different letter than the one it sends. Run `bash install.sh keys-probe` and read the characters. |
| panel shows one screen and never changes | the client is the one that rotates by default, so first check it is running and registered its screens: `bash install.sh status` and `journalctl -u pve-lcd.service | grep 'rotation'`. `screen rotation is driven by this client` means the client thinks it is rotating — look for a second client holding the same keys, or for the panel being stuck in LCDd's menu (press X, or the key mapped to `Escape`, to close it). `rotation is left to LCDd` means someone set `PVELCD_ROTATION=lcdproc`, and then `[server] AutoRotate=yes` in `/etc/LCDd.conf` is what makes the panel move: `bash install.sh fix-conf` writes it, `restart-lcd` applies it. |
| a button does nothing, or the log says `this client does not use that button` | the name the panel produced is not one the client asked for. `bash install.sh status` prints the `KeyMap_` lines and the `PVELCD_KEYS` line together — they must contain the same names. Rerun `bash install.sh keys-default` (it writes both) and `restart-lcd`. |
| up/down arrows do nothing | on the storage screen that usually means there is nothing to scroll: three or fewer volumes fit in the rows available, so the list is already showing all of it. The header only prints a position marker like `1-3/4` when more volumes exist than rows. |
| the storage screen shows 3 of my 4 volumes | that is the window, and it is meant to move: the header shows which slice is on the panel and the up/down arrows slide it one volume at a time. |
| the network screen says `no interface found` | this node has no default route and no interface with traffic on it, which usually means the panel is running on a machine that is not the one you meant. Set the interface you want explicitly with `PVELCD_NET_IFACE=vmbr0` in a drop-in. |
| the network screen shows `?` for bandwidth | the first sample after start has nothing to compare against, and an interface that changed name (a rename, or `PVELCD_NET_IFACE` pointing at something else) resets it. Within two refreshes it reads a rate. |
| the corner reads `\| Hold` / `\| Rotate` / `\| Prev` / `\| Next` and a row looks cut | that is the answer to the button you just pressed, and it is temporary: the word sits over the end of the last row for `PVELCD_NOTICE_SECONDS` (2.5 s) and the row comes back after it, with `>` where the word covered text. Set that variable to 0 if you would rather the corner stayed empty. |
| pressing the tick changes the panel but the corner says nothing | the press reached the client (the log says `Enter: screen held on the panel` or `screens rotate again`) but `PVELCD_NOTICE_SECONDS` is 0, or someone set `PVELCD_ROTATION=lcdproc` and LCDd is writing its own word instead. `bash install.sh status` prints both settings. |

## When the display server itself will not start

The client is only half of the setup: LCDd, the server, is the program that owns the
panel. If it is not running, the client can only wait — it logs `Connection refused`
forever and the panel shows its idle pattern. `bash install.sh status` then lists the
server service as `failed`.

    bash install.sh server-diag

This prints what `/usr/sbin/LCDd` actually is (a native Linux program, a script, or a
file that did not survive being copied between machines and cannot be executed), which
init scripts are supposed to start it and whether they carry Windows line endings, the
exact command systemd runs, what the server last logged, and the test that settles it:
running the server in the foreground with all of its own messages on screen.

If the server runs happily by hand but not at boot, the startup path is the broken part:

    bash install.sh server-unit

That writes `/etc/systemd/system/LCDd.service`, which takes precedence over the unit
systemd generates from `/etc/init.d/LCDd`, so the server comes up at boot without the old
scripts. It also switches off the boot-time start of `/etc/init.d/lcdproc`, so two servers
cannot fight over one serial port, and prints the two commands that undo all of it.
`restart-lcd` already tries this fallback on its own: if neither the service nor an init
script produces a running server, it starts `/usr/sbin/LCDd` directly and tells you that
the program is sound and the scripts are not.

Two things worth knowing about your host: both init scripts exist and neither belongs to
an installed package, and Debian's own copy of this stack is one package away
(`apt install lcdproc` provides `/usr/sbin/LCDd` plus the MtxOrb driver as
`/usr/lib/x86_64-linux-gnu/lcdproc/MtxOrb.so`). If you ever install it, delete our
`LCDd.service` and use the package's service instead — your `/etc/LCDd.conf` stays as it is.

Manual start and stop, if you prefer to drive it yourself:

    systemctl restart pve-lcd.service
    journalctl -fu pve-lcd.service

To remove it, keeping your config edits:

# Generated by Qwen 3.8 flash next running on Strata, slightly edited by me.

    bash install.sh uninstall
