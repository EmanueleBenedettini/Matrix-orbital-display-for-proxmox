#!/usr/bin/env bash
#
# install.sh - deploy pve-lcd on a Proxmox VE host and repair /etc/LCDd.conf
#
#   ./install.sh install          patch the config, install client + unit, start it
#   ./install.sh fix-conf         repair the config only (Size, ServerScreen, Heartbeat,
#                                 AutoRotate)
#   ./install.sh keys-probe       turn on keypad_test_mode so you can read button letters
#   ./install.sh keys-nav         left/right change the screen, up/down scroll the storage
#                                 list, tick holds the screen or lets the screens rotate;
#                                 the X key is left unmapped so it cannot open a menu the
#                                 arrows cannot walk
#   ./install.sh keys-default     the same, plus the X key opens LCDd's own menu
#   ./install.sh keys-menu C=Up H=Down D=Left E=Right I=Enter J=Escape
#                                 the same, and it also writes the [menu] key lines
#                                 (UpKey/DownKey/EnterKey/LeftKey/RightKey/MenuKey) so
#                                 LCDd's menu can actually be walked with the buttons
#   ./install.sh keys-nav C=Up H=Down D=Left E=Right I=Enter
#                                 any of these commands with the letters your own panel
#                                 sends (run keys-probe first to read them)
#   ./install.sh keys-map A=Right B=Left C=Enter     write KeyMap_ lines for your buttons
#
# KeyMap_<letter> is keyed on the character your panel sends, not on a position;
# keys-probe prints those characters.  Every keys-* command also writes
# /etc/systemd/system/pve-lcd.service.d/20-buttons.conf so the client asks LCDd for
# exactly the names the mapping produces, and patch_conf makes sure [server]
# AutoRotate=yes so the panel is allowed to change screens by itself.
#   ./install.sh restart-lcd      stop every LCDd server and start it fresh (needed after a config change)
#   ./install.sh server-diag      why the display server will not start: the program file itself,
#                                 the init scripts, line endings, and what systemd is running
#   ./install.sh server-unit      write a clean systemd service for LCDd so the display server
#                                 starts at boot without the two old init scripts
#   ./install.sh status           what is running, plus a preview at the panel's own size
#   ./install.sh measure          paint a ruler on the panel so you can count its real
#                                 columns (the client is stopped while it measures)
#   ./install.sh size 17x4        write the size you measured into [MtxOrb] and restart
#   ./install.sh logs             recent journal for the client and for LCDd
#   ./install.sh cpu-diag         who is costing CPU: what each unit burns, which
#                                 pvesh processes are running, who started them, and
#                                 how many pvesh calls this client actually makes
#   ./install.sh uninstall        remove the client and its unit (config edits are kept)
#
# Options may appear before or after the command:
#   --dry-run       show the config diff, change nothing
#   --backlight-no  also set hasAdjustableBacklight=no
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF=/etc/LCDd.conf
BIN=/usr/local/bin/pve-lcd.py
UNIT=/etc/systemd/system/pve-lcd.service
SERVICE=pve-lcd.service
# The buttons are half LCDd's key mapping and half the client's: the client has to ask
# LCDd for exactly the names the mapping produces, so every keys-* command writes this
# drop-in next to the config change it makes.
UNIT_DROPIN_DIR=/etc/systemd/system/$SERVICE.d
UNIT_DROPIN=$UNIT_DROPIN_DIR/20-buttons.conf
LCD_BIN=/usr/sbin/LCDd
SERVER_UNIT=/etc/systemd/system/LCDd.service
DESIRED_SIZE=20x4       # fallback when the config carries no Size at all
SIZE_FORCED=0           # set by ./install.sh size <WxH>

DRY=0
BACKLIGHT_NO=0
PROBE_KEYS=0
NAV_KEYS=0
MENU_ON=0
KEYMAP=""

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

need_root() {
  [ "$(id -u)" -eq 0 ] || die "run as root (this edits /etc/LCDd.conf and installs a systemd unit)"
}

need_conf() {
  [ -f "$CONF" ] || die "$CONF not found - is LCDproc installed? (apt install lcdproc)"
}

need_sources() {
  [ -f "$SCRIPT_DIR/pve-lcd.py" ] || die "pve-lcd.py not found next to install.sh"
  [ -f "$SCRIPT_DIR/pve-lcd.service" ] || die "pve-lcd.service not found next to install.sh"
}

# Count a setting inside the [MtxOrb] section only (Size and keypad_test_mode
# are driver options, and other sections could carry a same-named setting).
count_in_mtx() {
  awk -v want="$1" '
    BEGIN { want = tolower(want) }
    tolower($0) ~ /^\[mtxorb\]/ { in_m = 1; next }
    in_m && /^[[:space:]]*\[/   { in_m = 0 }
    in_m && tolower($0) ~ ("^[ \t]*" want "[ \t]*=") { found = 1 }
    END { print (found ? 1 : 0) }' "$CONF"
}

# grep -c prints 0 *and* exits non-zero when nothing matched, so a "|| echo 0"
# fallback behind it produces two lines and breaks the arithmetic below.
count_in_conf() {
  local number
  number=$(grep -ciE "$1" "$CONF" 2>/dev/null) || true
  number=${number//[^0-9]/}
  if [ -z "$number" ]; then number=0; fi
  printf '%s' "$number"
}

# The Size= value the [MtxOrb] section already carries (20x4 when it has none).
# The patcher writes this value back, so a size you measured is never silently
# replaced by the 20x4 default the next time you run install or keys-nav.
config_size() {
  local value
  value=$(awk '
      tolower($0) ~ /^\[mtxorb\]/ { in_m = 1; next }
      in_m && /^[[:space:]]*\[/   { in_m = 0 }
      in_m && tolower($0) ~ /^[ \t]*size[ \t]*=/ {
        sub(/\r$/, ""); sub(/^[^=]*=[ \t]*/, ""); sub(/[ \t].*/, ""); print; exit
      }' "$CONF" 2>/dev/null) || true
  value=${value//[^0-9xX]/}
  if [ -z "$value" ]; then value=20x4; fi
  printf '%s' "$value"
}

# ---------------------------------------------------------------- config patch

# LCDd's own menu is driven by the [menu] section, and every name written there has
# to be a name the driver really reports - that is, one of the names on the right
# hand side of a KeyMap_ line. With only MenuKey present the menu opens and then
# nothing inside it can be moved, which is exactly what "I can open it but the
# buttons do nothing in it" means. UpKey/DownKey walk the list, EnterKey selects,
# LeftKey/RightKey change a value (contrast, backlight), MenuKey leaves it.
# The names are derived from the mapping the caller supplied, so the [menu] section
# can never reference a key the panel is unable to send.
menu_lines_from_keymap() {
  local pair name low
  local v_menu="" v_enter="" v_up="" v_down="" v_left="" v_right=""
  for pair in $KEYMAP; do
    case "$pair" in
      *=*) : ;;
      *) continue ;;
    esac
    name=${pair#*=}
    low=$(printf '%s' "$name" | tr 'A-Z' 'a-z')
    case "$low" in
      menu|escape) if [ -z "$v_menu" ];  then v_menu=$name;  fi ;;
      enter)       if [ -z "$v_enter" ]; then v_enter=$name; fi ;;
      up)          if [ -z "$v_up" ];    then v_up=$name;    fi ;;
      down)        if [ -z "$v_down" ];  then v_down=$name;  fi ;;
      left)        if [ -z "$v_left" ];  then v_left=$name;  fi ;;
      right)       if [ -z "$v_right" ]; then v_right=$name; fi ;;
    esac
  done
  if [ -n "$v_menu" ];  then echo "MenuKey=$v_menu";   fi
  if [ -n "$v_enter" ]; then echo "EnterKey=$v_enter"; fi
  if [ -n "$v_up" ];    then echo "UpKey=$v_up";       fi
  if [ -n "$v_down" ];  then echo "DownKey=$v_down";   fi
  if [ -n "$v_left" ];  then echo "LeftKey=$v_left";   fi
  if [ -n "$v_right" ]; then echo "RightKey=$v_right"; fi
}

patch_conf() {
  need_conf
  local tmp
  tmp="$(mktemp)" || die "mktemp failed"

  # Keep whatever Size the config already carries unless `install.sh size` asked
  # for a specific one; the default 20x4 only applies when Size is missing.
  if [ "$SIZE_FORCED" -eq 0 ]; then
    DESIRED_SIZE="$(config_size)"
  fi
  say "display size for the layout: $DESIRED_SIZE"

  local has_serverscreen has_heartbeat has_size has_probe add_probe add_backlight has_backlight
  local has_menukey add_menukey has_prevscreen has_nextscreen has_autorotate add_autorotate
  local menu_lines menu_prev menu_next
  has_serverscreen=$(count_in_conf '^[[:space:]]*ServerScreen[[:space:]]*=')
  has_heartbeat=$(count_in_conf '^[[:space:]]*Heartbeat[[:space:]]*=')
  has_autorotate=$(count_in_conf '^[[:space:]]*AutoRotate[[:space:]]*=')
  has_size=$(count_in_mtx size)
  has_probe=$(count_in_mtx keypad_test_mode)
  has_backlight=$(count_in_mtx hasAdjustableBacklight)
  has_menukey=$(count_in_conf '^[[:space:]]*MenuKey[[:space:]]*=')
  has_prevscreen=$(count_in_conf '^[[:space:]]*PrevScreenKey[[:space:]]*=')
  has_nextscreen=$(count_in_conf '^[[:space:]]*NextScreenKey[[:space:]]*=')
  add_autorotate=$(( has_autorotate > 0 ? 0 : 1 ))

  # Menu mode: write the whole [menu] block from the mapping, and let [server] page
  # the screens with the names the panel can really produce.
  menu_lines=""
  menu_prev=""
  menu_next=""
  if [ "$MENU_ON" -eq 1 ]; then
    menu_lines=$(menu_lines_from_keymap)
    if [ -z "$menu_lines" ]; then
      warn "none of your key names is one LCDd's menu understands (Up Down Left Right Enter Escape); the menu would open but nothing inside it could move"
    fi
    menu_prev=$(printf '%s\n' "$menu_lines" | sed -n 's/^LeftKey=//p' | head -1)
    menu_next=$(printf '%s\n' "$menu_lines" | sed -n 's/^RightKey=//p' | head -1)
    if [ "$has_prevscreen" -gt 0 ]; then menu_prev=""; fi
    if [ "$has_nextscreen" -gt 0 ]; then menu_next=""; fi
    if [ -n "$menu_lines" ] && ! printf '%s\n' "$menu_lines" | grep -q '^MenuKey='; then
      warn "nothing in your mapping can open the menu: map the X key to Escape (for example J=Escape) so MenuKey has a name to match"
    fi
  fi
  menu_lines=$(printf '%s' "$menu_lines" | tr '\n' ';')

  add_menukey=0
  if [ "$MENU_ON" -eq 1 ] && [ "$has_menukey" -eq 0 ] && [ -z "$menu_lines" ]; then add_menukey=1; fi
  add_probe=0
  add_backlight=0
  if [ "$PROBE_KEYS" -eq 1 ] && [ "$has_probe" -eq 0 ]; then add_probe=1; fi
  if [ "$BACKLIGHT_NO" -eq 1 ] && [ "$has_backlight" -eq 0 ]; then add_backlight=1; fi

  awk \
    -v want_size="$DESIRED_SIZE" \
    -v add_size="$(( has_size > 0 ? 0 : 1 ))" \
    -v backlight_no="$BACKLIGHT_NO" \
    -v add_serverscreen="$(( has_serverscreen > 0 ? 0 : 1 ))" \
    -v add_heartbeat="$(( has_heartbeat > 0 ? 0 : 1 ))" \
    -v add_autorotate="$add_autorotate" \
    -v keymap="$KEYMAP" \
    -v probe_keys="$PROBE_KEYS" \
    -v add_probe="$add_probe" \
    -v add_backlight="$add_backlight" \
    -v nav_keys="$NAV_KEYS" \
    -v add_menukey="$add_menukey" \
    -v menu_nav="$MENU_ON" \
    -v menu_lines="$menu_lines" \
    -v server_prev="$menu_prev" \
    -v server_next="$menu_next" '
    BEGIN { sect = ""; srv_seen = 0; srv_added = 0; hb_added = 0; keymap_added = 0; mtx_seen = 0
             size_done = 0; bl_done = 0; probe_done = 0; menu_seen = 0; menukey_done = 0
             menu_done = 0; prev_added = 0; next_added = 0; ar_added = 0; ar_done = 0 }
    # Strip a trailing carriage return from every line before anything else looks
    # at it, so a config written on Windows is rewritten in unix line endings.
    { sub(/\r$/, "") }
    {
      raw = $0
      low = tolower(raw)

      if (low ~ /^[ \t]*\[/) {
        if (low ~ /^[ \t]*\[server\]/) {
          sect = "srv"; srv_seen = 1; print raw
          if (add_serverscreen && !srv_added) { print "ServerScreen=no"; srv_added = 1 }
          if (add_heartbeat && !hb_added) { print "Heartbeat=off"; hb_added = 1 }
          # AutoRotate is the one setting that decides whether the panel changes
          # screens by itself. Off is what makes the first screen sit there until
          # somebody presses the tick key.
          if (add_autorotate && !ar_added) { print "AutoRotate=yes"; ar_added = 1 }
          if (server_prev != "" && !prev_added) { print "PrevScreenKey=" server_prev; prev_added = 1 }
          if (server_next != "" && !next_added) { print "NextScreenKey=" server_next; next_added = 1 }
          next
        }
        if (low ~ /^[ \t]*\[mtxorb\]/) {
          sect = "mtx"; mtx_seen = 1; print raw
          if (add_size) { print "Size=" want_size }
          if (add_probe) { print "keypad_test_mode=yes" }
          if (add_backlight) { print "hasAdjustableBacklight=no" }
          if (keymap != "" && !keymap_added) {
            n = split(keymap, parts, " ")
            for (i = 1; i <= n; i++) {
              split(parts[i], kv, "=")
              if (kv[1] != "" && kv[2] != "") print "KeyMap_" kv[1] "=" kv[2]
            }
            keymap_added = 1
          }
          next
        }
        if (low ~ /^[ \t]*\[menu\]/) {
          sect = "menu"; menu_seen = 1; print raw
          if (add_menukey && !menukey_done) { print "MenuKey=Escape"; menukey_done = 1 }
          if (menu_nav && menu_lines != "" && !menu_done) {
            n = split(menu_lines, menu_parts, ";")
            for (i = 1; i <= n; i++) if (menu_parts[i] != "") print menu_parts[i]
            menu_done = 1
          }
          next
        }
        sect = ""
        print raw
        next
      }

      # An AutoRotate that is already there is rewritten rather than duplicated: the
      # panel starting on one screen and staying there is this one setting.
      if (sect == "srv" && low ~ /^[ \t]*autorotate[ \t]*=/) {
        if (ar_done) next
        print "AutoRotate=yes"; ar_done = 1; next
      }

      if (sect == "mtx") {
        if (low ~ /^[ \t]*size[ \t]*=/) {
          if (size_done) next
          print "Size=" want_size; size_done = 1; next
        }
        if (backlight_no && low ~ /^[ \t]*hasadjustablebacklight[ \t]*=/) {
          if (bl_done) next
          print "hasAdjustableBacklight=no"; bl_done = 1; next
        }
        # UpKey/MenuKey/... are not options of the MtxOrb driver. They belong in
        # [Menu] (with key names) or as KeyMap_<letter> here. Keep them visible
        # as comments so nothing is silently destroyed.
        if (low ~ /^[ \t]*(upkey|downkey|leftkey|rightkey|backkey|forwardkey|enterkey|menukey|escapekey)[ \t]*=/) {
          print "# install.sh moved this out of [MtxOrb] (invalid for this driver): " raw
          next
        }
        if (keymap != "" && low ~ /^[ \t]*keymap_/) { next }
        if (low ~ /^[ \t]*keypad_test_mode[ \t]*=/) {
          if (probe_done) next
          print "keypad_test_mode=" (probe_keys ? "yes" : "no"); probe_done = 1; next
        }
      }

      # The keys of the LCDd menu live in [menu], not in [MtxOrb]. In menu mode the
      # block written at the [menu] header above replaces whatever was there, so an
      # old line that names a key the panel cannot send is dropped instead of left
      # behind looking like it works. In paging mode the whole menu is cleared, so a
      # button cannot open a menu the buttons cannot walk.
      if (sect == "menu" && low ~ /^[ \t]*(menukey|enterkey|upkey|downkey|leftkey|rightkey)[ \t]*=/) {
        if (menu_nav && menu_lines != "") {
          if (menu_done) next
          print raw; menu_done = 1; next
        }
        if (nav_keys) {
          print "# install.sh cleared this [menu] key so the buttons only drive the pve-lcd screens: " raw
          next
        }
        print raw; next
      }

      print raw
    }
    END {
      if (!srv_seen) {
        printf "\n[server]\nServerScreen=no\nHeartbeat=off\nAutoRotate=yes\n"
        if (server_prev != "") print "PrevScreenKey=" server_prev
        if (server_next != "") print "NextScreenKey=" server_next
      }
      if (menu_nav && menu_lines != "" && !menu_done) {
        printf "\n[menu]\n"
        n = split(menu_lines, menu_parts, ";")
        for (i = 1; i <= n; i++) if (menu_parts[i] != "") print menu_parts[i]
      }
      else if (add_menukey && !menu_seen) {
        printf "\n[menu]\nMenuKey=Escape\n"
      }
      if (!mtx_seen) {
        if (keymap != "")
          print "install.sh note: this config has no [MtxOrb] section, so the KeyMap_ lines for your buttons were not written; check Driver= in [server]" > "/dev/stderr"
        else
          print "install.sh note: this config has no [MtxOrb] section; check Driver= in [server]" > "/dev/stderr"
      }
    }
  ' "$CONF" > "$tmp" || { rm -f "$tmp"; die "failed to rewrite $CONF"; }

  if diff -q "$CONF" "$tmp" >/dev/null 2>&1; then
    say "config already correct, nothing to change"
    rm -f "$tmp"
    return 0
  fi

  # When only the line endings were wrong, every single line differs and a normal
  # diff would show the whole file. Say what actually happened instead.
  if diff -q <(tr -d '\r' < "$CONF") "$tmp" >/dev/null 2>&1; then
    say "settings are already right; $CONF has Windows line endings that LCDd would read as part of the values"
  else
    say "changes to be made to $CONF:"
    diff -u "$CONF" "$tmp" | sed -n '3,80p'
  fi

  if [ "$DRY" -eq 1 ]; then
    say "dry run: $CONF left untouched"
    rm -f "$tmp"
    return 0
  fi

  local backup="$CONF.bak.$(date +%Y%m%d-%H%M%S)"
  cp -a "$CONF" "$backup" && say "backup written to $backup"
  cat "$tmp" > "$CONF" || { rm -f "$tmp"; die "could not write $CONF"; }
  rm -f "$tmp"
  say "$CONF patched"
  say "the LCDd that is running now still has the old settings loaded - run: ./install.sh restart-lcd"
}

# ---------------------------------------------------------------- diagnostics

# What kind of file a path really is. A program that is not a native Linux
# binary cannot be executed at all: systemd then refuses the whole service.
# This happens when a binary was built for another CPU, or when a binary was
# damaged while being copied through a text-only transfer.
binary_kind() {
  local file=$1 hex
  [ -f "$file" ] || { printf 'missing'; return 0; }
  hex=$(head -c 4 "$file" 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n')
  case "$hex" in
    7f454c46) printf 'ELF binary (a native Linux program)' ;;
    2321*)    printf 'a script for an interpreter' ;;
    *)        printf 'NOT a Linux program (first bytes: %s)' "${hex:-none}" ;;
  esac
}

# Count the lines in a text file that end with a carriage return. LCDd reads its
# config line by line; a leftover carriage return becomes part of the value it
# uses, which shows up as a device or a key name that looks correct but is not.
crlf_count() {
  local number
  number=$(grep -c $'\r' "$1" 2>/dev/null) || true
  number=${number//[^0-9]/}
  if [ -z "$number" ]; then number=0; fi
  printf '%s' "$number"
}

# systemctl is-active / is-enabled can answer with more than one line for units
# that came from an init script, which smashes the layout of a printf line.
unit_state() {
  local value
  value=$(systemctl "$1" "$2" 2>/dev/null | head -1 | tr -d '\n') || true
  if [ -z "$value" ]; then value=unknown; fi
  printf '%s' "$value"
}

count_lcd_servers() {
  local number
  number=$(pgrep -f '^/usr/sbin/LCDd' 2>/dev/null | grep -c .) || true
  number=${number//[^0-9]/}
  if [ -z "$number" ]; then number=0; fi
  printf '%s' "$number"
}

report_processes() {
  local servers clients
  servers=$(pgrep -fa '/usr/sbin/LCDd' 2>/dev/null | grep -v pgrep | wc -l)
  clients=$(pgrep -fa "$BIN" 2>/dev/null | grep -v pgrep | wc -l)
  say "LCDd servers running: $servers   pve-lcd clients running: $clients"
  if [ "$servers" -gt 1 ]; then
    warn "$servers LCDd processes are writing to the same serial port - only one may run."
    pgrep -fa '/usr/sbin/LCDd'
  elif [ "$servers" -eq 0 ]; then
    warn "no LCDd running: the display will show nothing but its idle pattern."
  fi
  if [ "$clients" -eq 0 ]; then
    warn "no client connected: LCDd can only show its own Clients:/Screens: counter."
  fi
}

report_launchers() {
  local matches
  matches=$(grep -ls '/usr/sbin/LCDd' /etc/init.d/* 2>/dev/null || true)
  if [ -n "$matches" ]; then
    say "init scripts that can start the LCDd server:"
    printf '   %s\n' $matches
    local count
    count=$(printf '%s\n' $matches | grep -c . )
    if [ "$count" -gt 1 ]; then
      warn "more than one init script starts LCDd; after a reboot you may end up with two servers."
    fi
  fi
}

# ---------------------------------------------------------------- commands

cmd_fix_conf() { patch_conf; }

cmd_install() {
  need_root
  need_sources
  patch_conf
  if [ "$DRY" -eq 1 ]; then
    say "dry run: would install $BIN and $UNIT, then enable and start $SERVICE"
    return 0
  fi

  say "installing $BIN"
  install -m 0755 "$SCRIPT_DIR/pve-lcd.py" "$BIN" || die "install failed"
  say "installing $UNIT"
  install -m 0644 "$SCRIPT_DIR/pve-lcd.service" "$UNIT" || die "install failed"

  # The stock sysv client would compete with our unit for the same screens.
  if [ -e /etc/init.d/lcdproc ]; then
    say "disabling the stock lcdproc client init script (pve-lcd replaces it)"
    update-rc.d lcdproc disable 2>/dev/null || warn "could not disable lcdproc via update-rc.d; check: systemctl list-units | grep lcd"
  fi

  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1 || warn "enable failed; it will still start manually"

  # If something already put KeyMap_ lines in the config, make the client ask for those
  # same names, so a plain ./install.sh install does not leave a working mapping behind a
  # client that never reserved it.  An existing 20-buttons.conf is left alone: it says
  # what the last keys-* command chose.
  local conf_keys
  conf_keys=$(names_from_conf)
  if [ -n "$conf_keys" ]; then
    if [ -f "$UNIT_DROPIN" ]; then
      say "the config maps buttons to: $conf_keys  ($UNIT_DROPIN already tells the client those names)"
    else
      say "the config maps buttons to: $conf_keys  - telling the client those names"
      apply_client_keys "$conf_keys"
    fi
  else
    say "no KeyMap_ lines in $CONF yet: the buttons do nothing until you map them (./install.sh keys-nav)"
  fi

  report_launchers
  report_processes
  if [ "$(count_lcd_servers)" -eq 0 ]; then
    warn "no display server is running: the client will keep retrying and show nothing until one starts. Find out why: ./install.sh server-diag"
  fi

  say "starting $SERVICE"
  systemctl restart "$SERVICE" || warn "start failed; run: journalctl -u $SERVICE -n 40"
  sleep 2
  "$BIN" --test --size "$DESIRED_SIZE" || warn "preview failed"
  cmd_status
}

cmd_keys_probe() {
  PROBE_KEYS=1
  patch_conf
  if [ "$DRY" -eq 1 ]; then return 0; fi
  say "keypad_test_mode is now yes. To read your buttons:"
  cat <<'EOF'
   1. stop every LCDd that is running:
        kill $(pgrep -f '/usr/sbin/LCDd') 2>/dev/null ; sleep 2
   2. run the server in the foreground so its messages land on your terminal:
        /usr/sbin/LCDd -f -c /etc/LCDd.conf
   3. press each button on the display one at a time.
      LCDd prints the character it received, e.g. a key from button 1 arrives as "C".
      If nothing shows on the terminal, watch the journal in another shell:
        journalctl -f | grep -iE 'key|mtxorb'
   4. Ctrl-C the server, then map the letters it printed: the letter you use in
      KeyMap_<letter> IS the character your panel sends, nothing else maps them.
        ./install.sh keys-nav C=Up H=Down D=Left E=Right I=Enter
      (Left/Right = previous/next screen, Up/Down = scroll the storage list,
       Enter = hold this screen or let the screens rotate again)
      Or keep LCDd's own menu usable; that mode also writes the [menu] key lines:
        ./install.sh keys-menu C=Up H=Down D=Left E=Right I=Enter J=Escape
      (the same, and while its menu is open Up/Down move in it, Enter selects a
       value and Escape opens or closes it)
      Leave the letter for a key alone and that key does nothing at all.
      A character outside A-Y cannot be mapped by this driver at all.
EOF
}

# The names already in the config, read straight out of its KeyMap_ lines.  Used by
# install and fix-conf so that the client is always asking for the names the panel can
# really produce, whatever put those lines there.
names_from_conf() {
  [ -f "$CONF" ] || return 0
  grep -E '^[[:space:]]*KeyMap_[A-Za-z][[:space:]]*=' "$CONF" 2>/dev/null \
    | sed 's/^[^=]*=//' | tr -d ' \t' \
    | awk '!seen[$0]++' | grep -viE '^(escape|menu)$' | tr '\n' ' ' \
    | sed 's/[[:space:]]*$//'
}

# Check pairs of <letter>=<KeyName>. The letter must be A-Y: MtxOrb only maps key
# characters in that range (MAX_KEY_MAP is 25); any other character it reports as
# "Untreated key" and throws away.
check_keymap() {
  for pair in "$@"; do
    case "$pair" in
      [A-Y]=*) : ;;
      *) die "bad pair '$pair': use <letter>=<KeyName>, e.g. C=Right" ;;
    esac
  done
  for pair in "$@"; do
    name=$(printf '%s' "${pair#*=}" | tr 'A-Z' 'a-z')
    case "$name" in
      [a-z0-9]) : ;;
      enter|left|right|up|down|menu|escape|backspace|tab|clear|insert|delete|home|end|pageup|pagedown|space) : ;;
      *) warn "key name '${pair#*=}' is not one LCDd normally understands (Enter Left Right Up Down Menu Escape)" ;;
    esac
  done
}

# The key names our KeyMap_ lines produce, without duplicates, in the order written.
# The client has to ask LCDd for exactly these names - a button whose name nobody asked
# for is handled by LCDd itself or dropped - so the mapping and the client are always
# written together.  Escape/Menu are left out: LCDd's menu holds those for itself and a
# client asking for one is refused.
names_from_keymap() {
  local pair name seen=" " out=""
  for pair in $KEYMAP; do
    name="${pair#*=}"
    case "$(printf '%s' "$name" | tr 'A-Z' 'a-z')" in
      escape|menu) continue ;;
    esac
    case "$seen" in
      *" $name "*) continue ;;
    esac
    seen="$seen$name "
    out="$out $name"
  done
  printf '%s' "${out# }"
}

# Write the client's half of the button setup and restart it.
apply_client_keys() {
  local keys=$1
  if [ -z "$keys" ]; then
    warn "none of the names in your mapping is a key this client uses, so it will not react to your buttons"
  fi
  if [ "$DRY" -eq 1 ]; then
    say "dry run: would write $UNIT_DROPIN_DIR/20-buttons.conf"
    printf '  [Service]\n  Environment=PVELCD_KEYS="%s"\n  Environment=PVELCD_ROTATION=client\n' "$keys"
    return 0
  fi
  mkdir -p "$UNIT_DROPIN_DIR" || die "could not create $UNIT_DROPIN_DIR"
  printf '[Service]\nEnvironment=PVELCD_KEYS="%s"\nEnvironment=PVELCD_ROTATION=client\n' "$keys" > "$UNIT_DROPIN" \
    || die "could not write $UNIT_DROPIN"
  systemctl daemon-reload || warn "systemctl daemon-reload failed"
  if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    systemctl restart "$SERVICE" \
      && say "$SERVICE restarted with PVELCD_KEYS=\"$keys\""
  else
    say "the client is not running; it will use PVELCD_KEYS=\"$keys\" when it starts"
  fi
}

cmd_keys_map() {
  if [ "$#" -lt 1 ]; then
    die "usage: install.sh keys-map C=Right D=Left I=Enter   (letter=KeyName)"
  fi
  need_root
  check_keymap "$@"
  KEYMAP="$*"
  PROBE_KEYS=0
  patch_conf
  apply_client_keys "$(names_from_keymap)"
  [ "$DRY" -eq 1 ] && return 0
  say "restart LCDd for the mapping to take effect"
}

# Paging mapping for a panel with four arrows, a tick and an X: the horizontal arrows
# change the screen, up/down move through a list that has more entries than the panel
# has rows (the storage screen is the one that does), the tick holds the current screen
# or lets it rotate on.  The client rotates the screens itself, so the panel starts
# changing them at boot without anyone pressing anything.  Whatever key you leave out
# of the mapping is not produced by MtxOrb at all, so it can never open a menu.
cmd_keys_nav() {
  need_root
  if [ "$#" -gt 0 ]; then
    check_keymap "$@"
    for pair in "$@"; do
      case "$(printf '%s' "${pair#*=}" | tr 'A-Z' 'a-z')" in
        escape|menu) warn "keys-nav leaves the X key to LCDd's menu; use keys-menu if you want to walk that menu with the buttons" ;;
      esac
    done
    KEYMAP="$*"
  else
    KEYMAP="C=Up H=Down D=Left E=Right I=Enter"
  fi
  NAV_KEYS=1
  PROBE_KEYS=0
  patch_conf
  apply_client_keys "$(names_from_keymap)"
  [ "$DRY" -eq 1 ] && return 0
  say "left/right arrows = previous/next screen, up/down arrows = scroll the storage list,"
  say "tick = hold this screen or let the screens rotate again"
  say "a key with no KeyMap_ line does nothing at all"
  say "restart LCDd for the mapping to take effect"
}

# Mapping that keeps LCDd's own menu reachable: the horizontal arrows change the
# screen, up/down scroll the storage list while a status screen is showing and walk
# LCDd's menu while the menu is showing, the tick selects, X opens it (Heartbeat,
# Backlight, TitleSpeed and MtxOrb Contrast).
cmd_keys_default() {
  need_root
  if [ "$#" -gt 0 ]; then
    check_keymap "$@"
    KEYMAP="$*"
  else
    KEYMAP="C=Up H=Down D=Left E=Right I=Enter J=Escape"
  fi
  MENU_ON=1
  PROBE_KEYS=0
  patch_conf
  apply_client_keys "$(names_from_keymap)"
  [ "$DRY" -eq 1 ] && return 0
  say "left/right change the screen; X opens the LCDd menu, up/down move in it, tick selects"
  say "restart LCDd for the mapping to take effect"
}

# The menu-capable mapping for a panel with four arrows, a tick and an X.
# The four arrows get four different names: back/forward change the screen, up/down
# scroll a list that is longer than the panel and walk LCDd's menu. Two buttons sharing
# one name cannot be told apart by LCDd, so that needs six distinct names for six
# buttons.
cmd_keys_menu() {
  need_root
  if [ "$#" -gt 0 ]; then
    check_keymap "$@"
    local pair name seen=" "
    for pair in "$@"; do
      name=$(printf '%s' "${pair#*=}" | tr 'A-Z' 'a-z')
      case "$seen" in
        *" $name "*)
          warn "two buttons produce the name $name; LCDd only receives the name, so inside its menu those two buttons do the same thing"
          ;;
      esac
      seen="$seen$name "
    done
    KEYMAP="$*"
  else
    KEYMAP="C=Up H=Down D=Left E=Right I=Enter J=Escape"
  fi
  MENU_ON=1
  NAV_KEYS=0
  PROBE_KEYS=0
  patch_conf
  apply_client_keys "$(names_from_keymap)"
  [ "$DRY" -eq 1 ] && return 0
  say "back/forward arrows change the screen (Left/Right)"
  say "up/down arrows scroll the storage list while a status screen is showing, and walk LCDd's menu while the menu is showing"
  say "tick selects a value in the menu, X opens and closes the menu"
  say "the menu holds: Options (Heartbeat, Backlight, TitleSpeed) and MtxOrb (Contrast, On/Off Brightness)"
  say "restart LCDd for the mapping to take effect"
}

cmd_status() {
  need_conf
  DESIRED_SIZE="$(config_size)"
  say "units"
  systemctl list-units --all --no-pager 2>/dev/null | grep -iE 'lcd' || echo "  (no lcd units known to systemd)"
  say "service states"
  for unit in LCDd.service lcdproc.service "$SERVICE"; do
    printf '  %-18s active=%-9s enabled=%s\n' "$unit" \
      "$(unit_state is-active "$unit")" "$(unit_state is-enabled "$unit")"
  done
  if [ -f "$SERVER_UNIT" ]; then
    say "our own service file for the display server is installed: $SERVER_UNIT"
  else
    say "the display server has no service file of ours; it depends on the init scripts below."
    if [ "$(count_lcd_servers)" -eq 0 ]; then
      warn "the display server is not running, so the client has nothing to talk to. Run: ./install.sh server-diag"
    fi
  fi
  report_processes
  report_launchers
  local endings
  endings=$(crlf_count "$CONF")
  if [ "$endings" -gt 0 ]; then
    warn "$CONF has $endings lines ending with a carriage return (Windows line endings); ./install.sh fix-conf rewrites them"
  fi
  say "effective LCDd settings"
  grep -nE '^[[:space:]]*(Driver|Device|Size|Type|Speed|Contrast|ServerScreen|Heartbeat|AutoRotate|WaitTime|ReportToSyslog|keypad_test_mode|KeyMap_[A-Za-z]|hasAdjustableBacklight|MenuKey|EnterKey|UpKey|DownKey|LeftKey|RightKey|PrevScreenKey|NextScreenKey|ToggleRotateKey|ScrollUpKey|ScrollDownKey)[[:space:]]*=' "$CONF" || true
  echo "   (ScrollUpKey/ScrollDownKey do nothing in LCDproc 0.5.9; the menu keys are the"
  echo "    UpKey/DownKey/EnterKey/LeftKey/RightKey/MenuKey lines inside the [menu] section)"
  say "what the client was told about the buttons (unit plus drop-ins)"
  if systemctl cat "$SERVICE" --no-pager 2>/dev/null | grep -q 'PVELCD_KEYS'; then
    systemctl cat "$SERVICE" --no-pager 2>/dev/null | grep 'Environment=PVELCD_KEYS\|Environment=PVELCD_ROTATION' || true
  else
    echo "   no PVELCD_KEYS line anywhere: the client uses its built-in default"
    echo "   (Up Down Left Right Enter) and rotates the screens itself. Run ./install.sh keys-nav"
    echo "   to write a line matching the names your KeyMap_ lines produce."
  fi
  if [ -x "$BIN" ]; then
    say "live preview of what the panel is showing right now (${DESIRED_SIZE})"
    "$BIN" --test --size "$DESIRED_SIZE" || warn "preview failed"
  fi
}

# ---------------------------------------------------------------- panel geometry

# Count the columns the panel really has.  The client service is stopped first so
# nothing repaints over the ruler, then it is started again whatever happens.
cmd_measure() {
  need_root
  need_conf
  [ -x "$BIN" ] || die "the client is not installed yet; run ./install.sh install first"

  say "what LCDd is writing right now"
  echo "   [MtxOrb] Size=$(config_size)"
  journalctl -u "$SERVICE" --no-pager 2>/dev/null | grep 'display is' | tail -1 \
    || echo "   (no client log line yet - run ./install.sh status)"

  local was_active=0
  if systemctl is-active --quiet "$SERVICE"; then
    was_active=1
    say "stopping $SERVICE so it cannot paint over the ruler"
    systemctl stop "$SERVICE"
  fi

  say "putting a ruler on the panel - look at the display and count it"
  "$BIN" --probe --hold 120 || warn "the probe could not reach LCDd; is the server running? (./install.sh restart-lcd)"

  if [ "$was_active" -eq 1 ]; then
    say "starting $SERVICE again"
    systemctl start "$SERVICE"
  fi

  say "reading the ruler: the top row numbers the columns 1, 2, 3 ... 9, 0, 1, 2 ..."
  echo "   a row that ends in 7 means the panel is 17 columns wide"
  echo "   the letter row: A is column 1, so a last letter of Q also means 17"
  echo "   the '-' row has a '+' at columns 1, 10 and 20; a missing '+' at the right"
  echo "   edge means the panel is narrower than the columns LCDd is writing"
  echo
  say "apply the number you counted with:"
  echo "   ./install.sh size 17x4"
}

cmd_size() {
  need_root
  need_conf
  local want="${1:-}"
  if [ -z "$want" ]; then
    die "usage: ./install.sh size 17x4   (count the columns first with ./install.sh measure)"
  fi
  printf '%s\n' "$want" | grep -qE '^[1-9][0-9]{0,1}x[1-9][0-9]{0,1}$' \
    || die "size must look like 17x4, 16x2 or 20x4 (run ./install.sh measure to find it)"
  local cols=${want%%x*} rows=${want##*x}
  if [ "$cols" -lt 8 ] || [ "$rows" -lt 1 ]; then
    die "$want is too small to draw anything useful on"
  fi

  DESIRED_SIZE="$want"
  SIZE_FORCED=1
  say "setting the display to ${cols} columns x ${rows} rows"
  patch_conf

  if [ -x "$BIN" ]; then
    say "preview at the new size"
    "$BIN" --test --size "$DESIRED_SIZE" || warn "preview failed"
  fi

  cmd_restart_lcd
}

cmd_logs() {
  say "journal for $SERVICE"
  journalctl -u "$SERVICE" -n 60 --no-pager 2>/dev/null | tail -60 || echo "  (nothing in the journal)"
  say "journal for LCDd (empty if the server is not managed by systemd)"
  journalctl -u LCDd -n 30 --no-pager 2>/dev/null | tail -30 || true
  say "syslog messages from LCDproc/LCDd"
  journalctl -b --no-pager 2>/dev/null | grep -iE 'lcdproc|LCDd|mtxorb' | tail -25 || echo "  (none found)"
}

# ---------------------------------------------------------------- CPU accounting

# Per-unit CPU is read from the cgroup, not from the client process: the
# expensive work here is done by child processes (pvesh), and systemd charges a
# child's CPU to the unit that started it.  Watching pve-lcd.py alone would
# report almost nothing while its children burn a core.
cgroup_usage_usec() {
  local file
  # unified cgroup (Proxmox 8) first, the cgroup-v1 layout (Proxmox 7) after it.
  for file in "/sys/fs/cgroup/system.slice/$1/cpu.stat" \
              "/sys/fs/cgroup/cpu/system.slice/$1/cpu.stat"; do
    if [ -r "$file" ] &&
       awk '/^usage_usec/ { print $2; found = 1 } END { if (!found) exit 1 }' "$file"; then
      return 0
    fi
  done
  return 1
}

parent_of() { ps -o ppid= -p "$1" 2>/dev/null | tr -d '[:space:]'; }
name_of()   { ps -o comm= -p "$1" 2>/dev/null | tr -d '[:space:]'; }

# The chain of processes above a pid, nearest first: what started this pvesh.
ancestry_of() {
  local pid=$1 parent name="" chain="" hops=0
  while [ -n "$pid" ] && [ "$pid" -gt 1 ] 2>/dev/null && [ "$hops" -lt 8 ]; do
    name=$(name_of "$pid")
    [ -n "$name" ] || break
    chain="$chain  <=  $name($pid)"
    pid=$(parent_of "$pid")
    hops=$((hops + 1))
  done
  printf '%s' "${chain#  }"
}

cmd_cpu_diag() {
  need_root
  local window=${CPU_DIAG_WINDOW:-40}
  case "$window" in (''|*[!0-9]*) window=40 ;; esac
  local units=("$SERVICE" LCDd.service)
  local unit ours sightings pid ppid pname total ours_count foreign_count client_main

  say "1. how many copies of the client are running (each one polls on its own)"
  report_processes

  say "2. what the units are configured to poll"
  for unit in "${units[@]}"; do
    local settings
    settings=$(systemctl show "$unit" --value -p Environment 2>/dev/null | tr '\n' ' ')
    if [ -n "$settings" ]; then
      echo "   $unit  $settings"
    else
      echo "   $unit  (not running, or no Environment= settings)"
    fi
  done
  cat <<'EOF'
   With no overrides this client runs pvesh twice every 30 seconds - the
   /nodes/<node>/qemu and /nodes/<node>/lxc lists behind the guests screen -
   and once every 180 seconds for the storage list: a little over 4 calls a
   minute.  CPU %, RAM, uptime, load and the cluster check come from /proc and
   /etc/pve and never call pvesh.  Repainting the panel every 2 seconds costs
   nothing but socket writes; only the polling rates cost CPU.
EOF

  say "3. measuring per-unit CPU for ${window} seconds while watching for pvesh"
  local before=() after=()
  for unit in "${units[@]}"; do before+=("$(cgroup_usage_usec "$unit" 2>/dev/null || true)"); done

  sightings=$(mktemp) || die "mktemp failed"
  ours=""
  local client_main
  client_main=$(systemctl show "$SERVICE" --value -p MainPID 2>/dev/null)
  case "$client_main" in (*[!0-9]*|'') client_main="" ;; esac
  for pid in $client_main $(pgrep -f "$BIN" 2>/dev/null); do ours="$ours $pid"; done

  local deadline=$((SECONDS + window))
  while [ "$SECONDS" -lt "$deadline" ]; do
    for pid in $(pgrep -f 'pvesh get' 2>/dev/null); do
      grep -q "^$pid " "$sightings" 2>/dev/null && continue
      ppid=$(parent_of "$pid")
      pname=$(name_of "$ppid")
      printf '%s %s %s\n' "$pid" "$ppid" "$pname" >> "$sightings"
    done
    sleep 1
  done
  for unit in "${units[@]}"; do after+=("$(cgroup_usage_usec "$unit" 2>/dev/null || true)"); done

  local i
  for i in "${!units[@]}"; do
    unit=${units[$i]}
    if [ -n "${before[$i]}" ] && [ -n "${after[$i]}" ]; then
      printf '   %-16s %5.1f%% of one core over these %ss   (%s CPU-seconds since the unit started)\n' \
        "$unit" \
        "$(awk -v a="${before[$i]}" -v b="${after[$i]}" -v w="$window" \
             'BEGIN { printf "%.1f", (b - a) * 100 / (w * 1000000) }')" \
        "$window" \
        "$(awk -v b="${after[$i]}" 'BEGIN { printf "%.1f", b / 1000000 }')"
    else
      printf '   %-16s no readable CPU accounting in /sys/fs/cgroup/system.slice/%s/cpu.stat\n' "$unit" "$unit"
    fi
  done

  say "4. every pvesh process seen in that window, and who started it"
  total=0 ours_count=0 foreign_count=0
  if [ ! -s "$sightings" ]; then
    say "not one pvesh process appeared during those ${window} seconds"
  else
    while read -r pid ppid pname; do
      total=$((total + 1))
      case "$ppid" in (''|*[!0-9]*) ppid=0 ;; esac
      case " $ours " in
        *" $ppid "*)
          ours_count=$((ours_count + 1))
          printf '   pid %-8s started by %s  <- ours, the panel client\n' "$pid" "${pname:-unknown}"
          ;;
        *)
          foreign_count=$((foreign_count + 1))
          local chain
          chain=$(ancestry_of "$ppid")
          [ -n "$chain" ] || chain="its parent had already exited before the sample"
          printf '   pid %-8s started by %s  <- NOT ours: %s\n' "$pid" "${pname:-unknown}" "$chain"
          ;;
      esac
    done < "$sightings"
    say "in ${window}s: $((total)) pvesh call(s) seen, ${ours_count} from the panel client, ${foreign_count} from something else"
    echo "   (a 1-second sampling interval can only be a floor: very short calls may be missed)"
  fi

  say "5. what the client logged about the cost of its own calls (last 30 minutes)"
  local calls
  calls=$(journalctl -u "$SERVICE" --since '-30 min' --no-pager 2>/dev/null | grep 'pvesh' | tail -25) || true
  if [ -n "$calls" ]; then
    printf '%s\n' "$calls"
  else
    echo "   (no pvesh lines: either the installed client predates this reporting,"
    echo "    or every call finished faster than PVELCD_SLOW_PVESH seconds)"
  fi

  cat <<EOF
   The clean separation test - stop the panel client and measure again:
     systemctl stop $SERVICE
     CPU_DIAG_WINDOW=40 ./install.sh cpu-diag     # anything still calling pvesh is not ours
     systemctl start $SERVICE

   If the panel client is the one costing CPU, lower its polling rates with an
   override (an override beats the defaults in the unit file, and survives the
   next ./install.sh install):
     mkdir -p /etc/systemd/system/$SERVICE.d
     printf '[Service]\nEnvironment=PVELCD_GUEST_TTL=90\nEnvironment=PVELCD_STORAGE_TTL=600\nEnvironment=PVELCD_GUEST_SOURCE=files\n' > /etc/systemd/system/$SERVICE.d/less-cpu.conf
     systemctl daemon-reload && systemctl restart $SERVICE
   GUEST_SOURCE=files drops the two guest pvesh calls completely, at the price
   of reading what pvedaemon last wrote to /etc/pve instead of asking reality.
EOF
  rm -f "$sightings"
}

# Everything needed to answer "why will the display server not start?".
cmd_server_diag() {
  need_conf
  say "1. the display server program itself"
  ls -l "$LCD_BIN" 2>/dev/null || warn "$LCD_BIN is not present"
  if [ -f "$LCD_BIN" ]; then
    echo "   format: $(binary_kind "$LCD_BIN")"
    if [ ! -x "$LCD_BIN" ]; then
      warn "it is not executable; fix with:  chmod 755 $LCD_BIN"
    fi
    if dpkg -S "$LCD_BIN" >/dev/null 2>&1; then
      echo "   owned by package: $(dpkg -S "$LCD_BIN" 2>/dev/null | head -1)"
    else
      echo "   no installed package owns it: this one was compiled or copied in by hand"
      echo "   Debian's own copy comes from  apt install lcdproc  (it also ships the MtxOrb driver)"
    fi
  fi
  say "2. what is supposed to start it"
  for init in /etc/init.d/LCDd /etc/init.d/lcdproc; do
    if [ -e "$init" ]; then
      echo "   $init   $(binary_kind "$init") | first line: $(head -1 "$init" | tr -d '\r') | lines ending with a carriage return: $(crlf_count "$init")"
    else
      echo "   $init   not present"
    fi
  done
  echo "   $CONF   lines ending with a carriage return: $(crlf_count "$CONF")"
  say "3. what systemd reports for the server unit"
  systemctl status LCDd.service --no-pager 2>/dev/null | sed -n '1,22p' || echo "   (no LCDd.service unit known)"
  say "4. the exact command systemd tries to run"
  systemctl cat LCDd.service --no-pager 2>/dev/null | sed -n '1,30p' || echo "   (nothing to show)"
  say "5. the last things the display server logged"
  journalctl -u LCDd.service -n 25 --no-pager 2>/dev/null | tail -25 || echo "   (nothing logged for that unit)"
  say "6. the test that settles it: run the server yourself"
  cat <<EOF
   release the display first:
       kill \$(pgrep -f '^/usr/sbin/LCDd') 2>/dev/null ; sleep 2
   then run it in the foreground with every message on screen (this is LCDd's own
   recommended way of debugging itself: -f foreground, -r 5 maximum reporting, -s 0 to stdout):
       $LCD_BIN -f -r 5 -s 0 -c $CONF
   Ctrl-C stops it again.
   If it runs happily this way, the program and the config are sound and only the
   startup scripts are broken  ->  ./install.sh server-unit gives it a service of its own.
   If it fails here too, the answer is in the messages it prints.
EOF
}

# A service file in /etc/systemd/system takes precedence over the unit systemd
# generates from /etc/init.d/LCDd, so this replaces the broken startup path
# without touching the init scripts themselves.
cmd_server_unit() {
  need_root
  [ -f "$LCD_BIN" ] || die "$LCD_BIN is not there: there is no server program to write a service for"
  local kind
  kind=$(binary_kind "$LCD_BIN")
  case "$kind" in
    ELF*) : ;;
    *)
      warn "$LCD_BIN is $kind"
      warn "no service file can make a program this machine cannot execute. The clean fix is Debian's server:  apt install lcdproc"
      die "nothing was written"
      ;;
  esac
  [ -x "$LCD_BIN" ] || warn "$LCD_BIN is not executable; run: chmod 755 $LCD_BIN"
  if [ "$DRY" -eq 1 ]; then
    say "dry run: would write $SERVER_UNIT, enable it and restart the server"
    return 0
  fi

  say "writing $SERVER_UNIT"
  cat > "$SERVER_UNIT" <<EOF
[Unit]
Description=LCDd - LCDproc display server
Documentation=man:LCDd(8)
After=network.target
Before=$SERVICE

[Service]
Type=simple
ExecStart=$LCD_BIN -f -c $CONF
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable LCDd.service >/dev/null 2>&1 || warn "enable failed, but it can still be started now"

  if [ -e /etc/init.d/lcdproc ]; then
    say "turning off the boot-time start of lcdproc's init script, so two servers cannot fight over the display"
    update-rc.d lcdproc disable 2>/dev/null || warn "could not disable it; put it back with: update-rc.d lcdproc enable"
  fi

  say "restarting the display server under the new service"
  systemctl restart LCDd.service || warn "it still failed - read why with: ./install.sh server-diag"
  sleep 2
  report_processes
  say "undo this with:  rm $SERVER_UNIT && systemctl daemon-reload && systemctl disable LCDd.service"
  say "if you ever install the Debian lcdproc package, delete this file and use its own service instead"
}

cmd_uninstall() {
  need_root
  systemctl disable --now "$SERVICE" 2>/dev/null || true
  rm -f "$UNIT" "$BIN" "$UNIT_DROPIN"
  rmdir "$UNIT_DROPIN_DIR" 2>/dev/null || true   # only removes it if it is now empty
  systemctl daemon-reload
  say "removed $SERVICE, $UNIT, $BIN and $UNIT_DROPIN (your LCDd.conf edits and backups were left alone)"
}

cmd_restart_lcd() {
  need_root
  need_conf
  local pattern="^/usr/sbin/LCDd" pids
  pids=$(pgrep -f "$pattern" 2>/dev/null) || true
  if [ -n "$pids" ]; then
    say "stopping LCDd server process(es): $(echo $pids | tr "\n" " ")"
    if [ "$DRY" -eq 1 ]; then
      say "dry run: no process stopped"
      return 0
    fi
    kill $pids 2>/dev/null || true
    sleep 2
    pids=$(pgrep -f "$pattern" 2>/dev/null) || true
    if [ -n "$pids" ]; then
      warn "this one is still running: $(echo $pids | tr "\n" " ")  (if it is a service that supervises it, that is fine)"
    fi
  else
    say "no LCDd server process is running"
  fi

  if [ "$DRY" -eq 1 ]; then
    say "dry run: would restart the LCDd service (systemd unit LCDd, else /etc/init.d/LCDd)"
    return 0
  fi

  if systemctl list-unit-files --no-pager 2>/dev/null | grep -q "^LCDd"; then
    say "systemctl restart LCDd"
    systemctl restart LCDd || warn "systemctl restart LCDd failed - start it by hand: /usr/sbin/LCDd -c $CONF &"
  elif [ -x /etc/init.d/LCDd ]; then
    say "/etc/init.d/LCDd restart"
    /etc/init.d/LCDd restart || warn "init script restart failed - start it by hand: /usr/sbin/LCDd -c $CONF &"
  else
    warn "no LCDd unit and no /etc/init.d/LCDd found: start the server yourself with   /usr/sbin/LCDd -c $CONF &"
  fi

  sleep 2
  if [ "$(count_lcd_servers)" -eq 0 ]; then
    warn "neither the systemd unit nor an init script produced a running server; starting $LCD_BIN directly"
    "$LCD_BIN" -c "$CONF" &
    sleep 3
    if [ "$(count_lcd_servers)" -gt 0 ]; then
      say "the server runs when started directly - so the program is sound and the startup scripts are the broken part."
      say "make that permanent with:   ./install.sh server-unit"
    else
      warn "$LCD_BIN did not stay running either. Read why:   ./install.sh server-diag"
      say "or run it in the foreground with maximum reporting and look at the errors on screen:"
      echo "   $LCD_BIN -f -r 5 -s 0 -c $CONF"
    fi
  fi
  report_processes
  say "the client finds the server again by itself within a few seconds"
}

usage() {
  sed -n '3,/^[^#]/p' "$0" | sed 's/^# \{0,1\}//; $d'
}

# ---------------------------------------------------------------- main

COMMAND=""
KEYMAP_ARGS=()
SIZE_ARGS=()
while [ "$#" -gt 0 ]; do
  arg="$1"
  shift
  case "$arg" in
    --dry-run) DRY=1 ;;
    --backlight-no) BACKLIGHT_NO=1 ;;
    install|fix-conf|restart-lcd|keys-probe|keys-map|keys-nav|keys-default|keys-menu|status|logs|cpu-diag|server-diag|server-unit|measure|size|uninstall|help|-h|--help) COMMAND="$arg" ;;
    *)
      case "$COMMAND" in
        keys-map|keys-nav|keys-default|keys-menu) KEYMAP_ARGS+=("$arg") ;;
        size) SIZE_ARGS+=("$arg") ;;
        *) die "unknown option: $arg  (options --dry-run/--backlight-no may come before or after the command)" ;;
      esac
      ;;
  esac
done

case "${COMMAND:-help}" in
  install)    cmd_install ;;
  fix-conf)   cmd_fix_conf ;;
  keys-probe) cmd_keys_probe ;;
  keys-map)   cmd_keys_map "${KEYMAP_ARGS[@]}" ;;
  keys-nav)   cmd_keys_nav "${KEYMAP_ARGS[@]}" ;;
  keys-default) cmd_keys_default "${KEYMAP_ARGS[@]}" ;;
  keys-menu)  cmd_keys_menu "${KEYMAP_ARGS[@]}" ;;
  restart-lcd) cmd_restart_lcd ;;
  server-diag) cmd_server_diag ;;
  server-unit) cmd_server_unit ;;
  status)     cmd_status ;;
  logs)       cmd_logs ;;
  cpu-diag)   cmd_cpu_diag ;;
  measure)    cmd_measure ;;
  size)       cmd_size "${SIZE_ARGS[@]}" ;;
  uninstall)  cmd_uninstall ;;
  help|-h|--help) usage ;;
esac
