import sys
import os
import time
import wifi
import socketpool
import gc
import board
import microcontroller
import storage
import busio
import analogio
import displayio
import rtc
import struct
import json

# --- GLOBAL VARS ---
kb = None
term = None
REPL_ENV = {}
SYSTEM_CONFIG = {}

# --- RECOVERY MODE ---
def recovery_mode(error_msg):
    print(f"\nCRASH: {error_msg}")
    global kb, term
    try:
        if not kb:
            from cardputeradvkey import Keyboard
            kb = Keyboard()
        if not term:
            from cardterm import Terminal
            term = Terminal()
    except:
        print("CRITICAL HARDWARE FAILURE")
        while True: pass

    try:
        if not term.display.root_group:
            term = Terminal()
    except: pass

    term.label_console.text = f"SYSTEM CRASH!\n{str(error_msg)[:100]}"
    term.label_console.color = 0xFF0000 
    term.label_prompt.text = "RECOVERY > "
    term.label_prompt.color = 0xFFA500
    term.label_input.text = "_"
    
    current_input = ""
    while True:
        char = kb.check()
        if char:
            if char == "ENTER":
                parts = current_input.split(" ")
                cmd = parts[0]
                args = parts[1:]
                try:
                    if cmd == "nano":
                        term.print("Loading Editor...", 0x00FFFF)
                        from code import cmd_nano
                        cmd_nano(args)
                    elif cmd == "ls": term.print(" ".join(os.listdir("/")))
                    elif cmd == "reboot": microcontroller.reset()
                    elif cmd == "help": term.print("ls nano reboot")
                    else: term.print("Unknown.")
                except Exception as e: term.print(f"Err: {e}", 0xFF0000)
                current_input = ""; term.label_input.text = "_"
            elif char == "DEL": 
                current_input = current_input[:-1]
                term.label_input.text = current_input + "_"
            elif len(char) == 1 or char == "SPACE":
                if char == "SPACE": char = " "
                current_input += char
                term.label_input.text = current_input + "_"
        time.sleep(0.01)

# --- VIRTUAL IO FUNCTIONS ---
def virtual_print(*args, sep=" ", end="\n"):
    text = sep.join(str(a) for a in args)
    if term: term.print(text)
    else: print(text)

def virtual_input(prompt=""):
    global kb, term
    if prompt: term.print(prompt, 0x00FFFF)
    user_string = ""; cursor_pos = 0
    while True:
        char = kb.check()
        if char:
            if char == "ENTER":
                term.print(f"> {user_string}", 0x555555)
                term.label_input.text = "_"
                return user_string
            elif char == "LEFT":
                if cursor_pos > 0: cursor_pos -= 1
            elif char == "RIGHT":
                if cursor_pos < len(user_string): cursor_pos += 1
            elif char == "DEL": 
                if cursor_pos > 0:
                    user_string = user_string[:cursor_pos-1] + user_string[cursor_pos:]
                    cursor_pos -= 1
            elif len(char) == 1 or char == "SPACE":
                ins = " " if char == "SPACE" else char
                user_string = user_string[:cursor_pos] + ins + user_string[cursor_pos:]
                cursor_pos += len(ins)
            
            vis_str = user_string[:cursor_pos] + "_" + user_string[cursor_pos:]
            if len(vis_str) > 28:
                start = max(0, cursor_pos - 14)
                if start + 28 > len(vis_str): start = max(0, len(vis_str) - 28)
                disp = vis_str[start : start+28]
                if start > 0: disp = "." + disp[1:]
                term.label_input.text = disp
            else: term.label_input.text = vis_str
        time.sleep(0.01)

# --- CORE KERNEL LOGIC ---
def update_prompt():
    c_user = globals()['CURRENT_USER']
    cwd = globals()['CWD']
    color = 0xFF5555 if c_user == "root" else 0x00FFFF
    globals()['PROMPT_CHAR'] = "#" if c_user == "root" else "$"
    home = globals()['ROOT_HOME'] if c_user == "root" else globals()['GUEST_HOME']
    if cwd.startswith(home): cwd = "~" + cwd[len(home):]
    term.label_prompt.color = color
    term.label_prompt.text = f"{c_user} {cwd} {globals()['PROMPT_CHAR']} "
    term.label_input.x = term.label_prompt.x + (len(term.label_prompt.text) * 6)

def resolve_path(path):
    cwd = globals()['CWD']
    if path == "/": return "/"
    if path.startswith("~"):
        home = globals()['ROOT_HOME'] if globals()['CURRENT_USER'] == "root" else globals()['GUEST_HOME']
        path = home + path[1:]
    target = path if path.startswith("/") else (cwd + "/" + path if cwd != "/" else "/" + path)
    parts = [p for p in target.split("/") if p != ""]
    final = []
    for p in parts:
        if p == "..": 
            if final: final.pop()
        elif p != ".": final.append(p)
    return "/" + "/".join(final)

def file_exists(path):
    try: os.stat(path); return True
    except: return False

def is_dir(path):
    try: return (os.stat(path)[0] & 0x4000) != 0
    except: return False

def find_executable(cmd_name):
    if "/" in cmd_name:
        if file_exists(resolve_path(cmd_name)): return resolve_path(cmd_name)
        return None
    
    local = resolve_path(cmd_name)
    if file_exists(local) and local.endswith(".pbash"): return local
    
    local_py = resolve_path(cmd_name if cmd_name.endswith(".py") else cmd_name + ".py")
    if file_exists(local_py): return local_py
    
    clean = cmd_name[:-3] if cmd_name.endswith(".py") else cmd_name
    for folder in globals()['SYSTEM_PATH']:
        try:
            target = f"{folder}/{clean}.py"
            if file_exists(target): return target
        except: continue
    return None

def check_write_access(target_path):
    if globals()['CURRENT_USER'] == "root": return True
    if target_path.startswith(globals()['GUEST_HOME']): return True
    term.print("Permission Denied (Guest)", 0xFF0000)
    return False

def tab_complete(partial_cmd):
    parts = partial_cmd.split(" ")
    if not parts: return partial_cmd
    target = parts[-1]
    base_path = globals()['CWD']
    search_prefix = target
    if "/" in target:
        parent = target[:target.rfind("/")+1]
        search_prefix = target[target.rfind("/")+1:]
        search_dir = resolve_path(parent)
    else: search_dir = base_path
    try:
        candidates = []
        for item in os.listdir(search_dir):
            if item.startswith(search_prefix): candidates.append(item)
        if candidates:
            match = sorted(candidates)[0]
            if "/" in target: completed_path = target[:target.rfind("/")+1] + match
            else: completed_path = match
            parts[-1] = completed_path
            return " ".join(parts)
    except: pass
    return partial_cmd

def run_command_line(cmd_str):
    if not cmd_str.strip() or cmd_str.startswith("#"): return
    parts = cmd_str.strip().split(" ")
    cmd = parts[0]
    args = parts[1:]
    
    if cmd in COMMANDS:
        COMMANDS[cmd](args)
        return

    exec_path = find_executable(cmd)
    
    if exec_path:
        if exec_path.endswith(".pbash"):
            cmd_pbash([exec_path] + args)
        else:
            try:
                with open(exec_path, "r") as f:
                    if not hasattr(sys, "argv"): sys.argv = []
                    while len(sys.argv) > 0: sys.argv.pop()
                    sys.argv.append(exec_path)
                    for a in args: sys.argv.append(a)
                    exec(f.read(), REPL_ENV)
                    if term.display.root_group != term.splash:
                        term.display.root_group = term.splash
            except Exception as e: term.print(f"Exec Err: {e}", 0xFF0000)
        return

    try:
        res = eval(cmd_str, REPL_ENV)
        if res is not None: term.print(str(res))
    except:
        try: exec(cmd_str, REPL_ENV)
        except Exception as e: term.print(f"Err: {e}", 0xFF0000)

def run_script_file(path):
    path = resolve_path(path)
    try:
        with open(path, "r") as f:
            lines = f.readlines()
            for line in lines:
                l = line.strip()
                if l: run_command_line(l)
    except Exception as e:
        term.print(f"Script Err: {e}", 0xFF0000)

# --- CONFIG MANAGEMENT ---
def load_config():
    default_config = {
        "users": {"root": "pbash", "guest": ""},
        "wifi": {}
    }
    try:
        if file_exists("/config.json"):
            with open("/config.json", "r") as f:
                conf = json.load(f)
                # Merge defaults
                if "users" not in conf: conf["users"] = default_config["users"]
                if "wifi" not in conf: conf["wifi"] = {}
                return conf
    except: pass
    return default_config

def save_config(conf):
    try:
        with open("/config.json", "w") as f:
            json.dump(conf, f)
        return True
    except OSError:
        # Fails silently if Read-Only, user sees visual feedback in cmd
        return False

# --- COMMANDS ---
def cmd_echo(args):
    term.print(" ".join(args))

def cmd_sleep(args):
    if not args: return
    try:
        sec = float(args[0])
        time.sleep(sec)
    except: pass

def cmd_pbash(args):
    if not args: return term.print("Usage: pbash <file>")
    run_script_file(args[0])

def cmd_ntp(args):
    if not wifi.radio.ipv4_address: return term.print("No WiFi", 0xFF0000)
    offset = 0
    if args:
        try: offset = int(args[0])
        except: pass
    term.print(f"Syncing (UTC{offset:+})...", 0x00FFFF)
    try:
        pool = socketpool.SocketPool(wifi.radio)
        packet = bytearray(48); packet[0] = 0x1B
        with pool.socket(pool.AF_INET, pool.SOCK_DGRAM) as sock:
            sock.settimeout(5)
            sock.sendto(packet, ("pool.ntp.org", 123))
            size, addr = sock.recvfrom_into(packet)
            ntp_seconds = struct.unpack("!I", packet[40:44])[0]
            timestamp = ntp_seconds - 2208988800 + (offset * 3600)
            rtc.RTC().datetime = time.localtime(timestamp)
            term.print("Time Set!", 0x00FF00)
            cmd_time([])
    except Exception as e: term.print(f"NTP Err: {e}", 0xFF0000)

def cmd_time(args):
    t = time.localtime()
    uptime_s = int(time.monotonic())
    time_str = "{:02}:{:02}:{:02}".format(t.tm_hour, t.tm_min, t.tm_sec)
    date_str = "{}/{}/{}".format(t.tm_mon, t.tm_mday, t.tm_year)
    mins, secs = divmod(uptime_s, 60); hours, mins = divmod(mins, 60)
    term.print(f"Time: {time_str} ({date_str})", 0x00FFFF)
    term.print(f"Up:   {hours}h {mins}m {secs}s", 0x00FF00)

def cmd_rm(args):
    if not args: return
    recursive = False; targets = []
    for arg in args:
        if arg == "-rf" or arg == "-r": recursive = True
        else: targets.append(arg)
    if not targets: return
    p = resolve_path(targets[0])
    if not check_write_access(p): return
    
    def rm_recursive(path):
        if is_dir(path):
            for child in os.listdir(path):
                child_path = path + "/" + child if path != "/" else "/" + child
                rm_recursive(child_path)
            try: os.rmdir(path)
            except: pass
        else: os.remove(path)

    try:
        if is_dir(p):
            if recursive:
                term.print(f"Del dir {targets[0]}...", 0xFFA500)
                rm_recursive(p)
                term.print("Deleted.")
            else:
                try: os.rmdir(p); term.print(f"Deleted dir {targets[0]}")
                except: term.print("Use -rf", 0xFF0000)
        else:
            os.remove(p); term.print(f"Deleted {targets[0]}")
    except Exception as e: term.print(f"Fail: {e}", 0xFF0000)

def cmd_mkdir(args):
    if not args: return
    p = resolve_path(args[0])
    if not check_write_access(p): return
    try: os.mkdir(p); term.print(f"Created {args[0]}")
    except: term.print("Fail", 0xFF0000)

def cmd_help(args):
    term.print("Available Commands:", 0x00FFFF)
    keys = sorted(COMMANDS.keys())
    term.print(" ".join(keys))

def cmd_disk(args):
    try:
        path = globals()['CWD']
        stats = os.statvfs(path)
        bsize = stats[0]; total = stats[2] * bsize; free = stats[3] * bsize; used = total - free
        percent = (used/total)*100 if total > 0 else 0
        def fmt(b):
            if b >= 1024*1024: return f"{b/(1024*1024):.1f} MB"
            if b >= 1024: return f"{b/1024:.1f} KB"
            return f"{b} B"
        color = 0x00FF00
        if percent > 80: color = 0xFFA500
        if percent > 95: color = 0xFF0000
        term.print(f"Path: {path}", 0x00FFFF)
        term.print(f"Size: {fmt(total)}")
        term.print(f"Used: {fmt(used)} ({percent:.0f}%)", color)
        term.print(f"Free: {fmt(free)}", 0x00FF00)
    except Exception as e: term.print(f"Disk Err: {e}", 0xFF0000)

def cmd_python(args):
    term.print("Python REPL (ESC to exit)", 0x00FF00)
    old_prompt = term.label_prompt.text; old_color = term.label_prompt.color
    term.label_prompt.text = ">>> "; term.label_prompt.color = 0x00FF00
    term.label_input.x = 28; term.label_input.text = "_"
    repl_input = ""; cursor_pos = 0; running_repl = True
    while running_repl:
        char = kb.check()
        if char:
            if char == "ESCAPE": running_repl = False
            elif char == "ENTER":
                term.print(f">>> {repl_input}", 0x555555)
                try:
                    try:
                        res = eval(repl_input, REPL_ENV)
                        if res is not None: term.print(str(res))
                    except SyntaxError: exec(repl_input, REPL_ENV)
                except Exception as e: term.print(f"Err: {e}", 0xFF0000)
                repl_input = ""; cursor_pos = 0
            elif char == "LEFT":
                if cursor_pos > 0: cursor_pos -= 1
            elif char == "RIGHT":
                if cursor_pos < len(repl_input): cursor_pos += 1
            elif char == "DEL":
                if cursor_pos > 0:
                    repl_input = repl_input[:cursor_pos-1] + repl_input[cursor_pos:]
                    cursor_pos -= 1
            elif len(char) == 1 or char == "SPACE":
                ins = " " if char == "SPACE" else char
                repl_input = repl_input[:cursor_pos] + ins + repl_input[cursor_pos:]
                cursor_pos += 1
            vis = repl_input[:cursor_pos] + "_" + repl_input[cursor_pos:]
            if len(vis) > 28:
                start = max(0, cursor_pos - 14)
                if start + 28 > len(vis): start = max(0, len(vis) - 28)
                disp = vis[start:start+28]
                if start > 0: disp = "." + disp[1:]
                term.label_input.text = disp
            else: term.label_input.text = vis
        time.sleep(0.005)
    term.label_prompt.text = old_prompt; term.label_prompt.color = old_color
    update_prompt()
    term.print("Exited REPL.")

def cmd_battery(args):
    try:
        adc = analogio.AnalogIn(board.IO10)
        voltage = (adc.value * 3.3 / 65535) * 2
        percent = (voltage - 3.2) / (4.2 - 3.2) * 100
        if percent > 100: percent = 100
        if percent < 0: percent = 0
        color = 0x00FF00
        if percent < 50: color = 0xFFA500
        if percent < 20: color = 0xFF0000
        term.print(f"Bat: {percent:.0f}% ({voltage:.2f}V)", color)
        adc.deinit()
    except Exception as e: term.print(f"Bat Err: {e}", 0xFF0000)

def cmd_ls(args):
    path_arg = None
    for arg in args:
        if not arg.startswith("-"): path_arg = arg
    target = resolve_path(path_arg if path_arg else globals()['CWD'])
    try:
        items = os.listdir(target); dirs = []; files = []
        for item in sorted(items):
            full = target + "/" + item if target != "/" else "/" + item
            try:
                if os.stat(full)[0] & 0x4000: dirs.append(item + "/")
                else: files.append(item)
            except: files.append(item)
        if dirs: term.print("  ".join(dirs), 0x00FFFF)
        if files: term.print("  ".join(files), 0x00FF00)
    except OSError: term.print(f"Err {target}", 0xFF0000)

def cmd_cd(args):
    target = globals()['GUEST_HOME'] if globals()['CURRENT_USER'] == "guest" else globals()['ROOT_HOME']
    if args: target = resolve_path(args[0])
    try: 
        os.listdir(target); globals()['CWD'] = target
    except: term.print("Invalid dir", 0xFF0000)
    update_prompt()

def cmd_cat(args):
    if args:
        try:
            with open(resolve_path(args[0]), "r") as f: term.print(f.read())
        except: term.print("Read Error", 0xFF0000)

def cmd_cp(args):
    if len(args) < 2: return
    src, dst = resolve_path(args[0]), resolve_path(args[1])
    if not check_write_access(dst): return
    try:
        with open(src, "r") as fs, open(dst, "w") as fd: fd.write(fs.read())
        term.print("Copied")
    except Exception as e: term.print(f"Err: {e}", 0xFF0000)

def cmd_mv(args):
    if len(args) < 2: return
    src, dst = resolve_path(args[0]), resolve_path(args[1])
    if not check_write_access(src) or not check_write_access(dst): return
    try: os.rename(src, dst); term.print("Moved")
    except: term.print("Fail", 0xFF0000)

def cmd_touch(args):
    if not args: return
    p = resolve_path(args[0])
    if not check_write_access(p): return
    try: 
        with open(p, "a"): pass
        term.print(f"Touched {args[0]}")
    except: term.print("Fail", 0xFF0000)

def cmd_nano(args):
    if not args: return term.print("Usage: nano <file>")
    filename = resolve_path(args[0])
    can_write = False
    if globals()['CURRENT_USER'] == "root": can_write = True
    elif filename.startswith(globals()['GUEST_HOME']): can_write = True
    lines = [""]
    try:
        with open(filename, "r") as f:
            content = f.read(); lines = content.split("\n") if content else [""]
    except: pass
    cx, cy, scroll_y, scroll_x = 0, 0, 0, 0
    running = True; term.label_prompt.text = "" 
    while running:
        SCREEN_W = 35
        if cx < scroll_x: scroll_x = cx
        if cx >= scroll_x + SCREEN_W: scroll_x = cx - SCREEN_W
        view = lines[scroll_y : scroll_y + 9]
        disp = ""
        for i, l in enumerate(view):
            if (scroll_y + i) == cy:
                line_with_cursor = l[:cx] + "|" + l[cx:]
                visible_part = line_with_cursor[scroll_x : scroll_x + 37]
                disp += f">{visible_part}\n"
            else:
                visible_part = l[scroll_x : scroll_x + 38]
                disp += f" {visible_part}\n"
        term.label_console.text = disp
        status = f"CTRL:Save ESC:Exit | {cy+1}:{cx}" if can_write else f"[RO] ESC:Exit | {cy+1}:{cx}"
        term.label_input.text = status; term.label_input.x = 20
        char = kb.check()
        if char:
            if char == "ESCAPE": running = False
            elif char == "UP":
                if cy > 0: cy -= 1
                if cy < scroll_y: scroll_y -= 1
            elif char == "DOWN":
                if cy < len(lines) - 1: cy += 1
                if cy >= scroll_y + 9: scroll_y += 1
            elif char == "LEFT":
                if cx > 0: cx -= 1
            elif char == "RIGHT":
                if cx < len(lines[cy]): cx += 1
            elif can_write:
                if char == "ENTER":
                    rem = lines[cy][cx:]; lines[cy] = lines[cy][:cx]
                    lines.insert(cy + 1, rem); cy += 1; cx = 0; scroll_x = 0
                elif char == "DEL":
                    if cx > 0: lines[cy] = lines[cy][:cx-1] + lines[cy][cx:]; cx -= 1
                    elif cy > 0: cx = len(lines[cy-1]); lines[cy-1] += lines[cy]; del lines[cy]; cy -= 1
                elif char == "CTRL": 
                    try:
                        with open(filename, "w") as f: f.write("\n".join(lines))
                        term.label_input.text = "SAVED!"; time.sleep(0.5)
                    except: term.label_input.text = "ERR: Read-Only?"
                elif char == "TAB": lines[cy] = lines[cy][:cx] + "  " + lines[cy][cx:]; cx += 2
                elif len(char) == 1: lines[cy] = lines[cy][:cx] + char + lines[cy][cx:]; cx += 1
        time.sleep(0.01)
    term.clear(); update_prompt()

# --- AUTH COMMANDS ---
def cmd_su(args):
    target_user = "root"
    if args: target_user = args[0]
    if target_user not in SYSTEM_CONFIG["users"]: return term.print("User not found.", 0xFF0000)
    if globals()['CURRENT_USER'] == target_user: return term.print(f"Already {target_user}.")
    term.print(f"Password for {target_user}: ", 0xFFFF00)
    pwd = ""; term.label_input.text = ""
    while True:
        c = kb.check()
        if c:
            if c == "ENTER": break
            elif c == "DEL": pwd = pwd[:-1]
            elif len(c) == 1: pwd += c
            term.label_input.text = "*" * len(pwd) + "_"
        time.sleep(0.01)
    if pwd == SYSTEM_CONFIG["users"][target_user]:
        globals()['CURRENT_USER'] = target_user
        try: 
            home = f"/home/{target_user}"
            if target_user == "root": home = globals()['ROOT_HOME']
            if target_user == "guest": home = globals()['GUEST_HOME']
            os.stat(home); globals()['CWD'] = home
        except: pass
        term.print("Access Granted.", 0x00FF00)
    else: term.print("Auth Failure", 0xFF0000)
    update_prompt()

def cmd_adduser(args):
    if globals()['CURRENT_USER'] != "root": return term.print("Root required.", 0xFF0000)
    if not args: return term.print("Usage: adduser <name>")
    new_user = args[0]
    if new_user in SYSTEM_CONFIG["users"]: return term.print("User exists.")
    term.print(f"Set password for {new_user}: ", 0x00FFFF)
    pwd = ""; term.label_input.text = ""
    while True:
        c = kb.check()
        if c:
            if c == "ENTER": break
            elif c == "DEL": pwd = pwd[:-1]
            elif len(c) == 1: pwd += c
            term.label_input.text = "*" * len(pwd) + "_"
        time.sleep(0.01)
    SYSTEM_CONFIG["users"][new_user] = pwd
    if save_config(SYSTEM_CONFIG):
        try: os.mkdir(f"/home/{new_user}"); term.print(f"User {new_user} created.", 0x00FF00)
        except: term.print("Created (No Home Dir)", 0xFFA500)
    else: term.print("Save Failed", 0xFF0000)

def cmd_passwd(args):
    c_user = globals()['CURRENT_USER']
    term.print(f"New Password ({c_user}): ", 0x00FFFF)
    pwd = ""; term.label_input.text = ""
    while True:
        c = kb.check()
        if c:
            if c == "ENTER": break
            elif c == "DEL": pwd = pwd[:-1]
            elif len(c) == 1: pwd += c
            term.label_input.text = "*" * len(pwd) + "_"
        time.sleep(0.01)
    SYSTEM_CONFIG["users"][c_user] = pwd
    if save_config(SYSTEM_CONFIG): term.print("Password updated.", 0x00FF00)
    else: term.print("Save Failed.", 0xFF0000)

def cmd_logout(args):
    globals()['CURRENT_USER'] = "guest"
    try: os.stat(globals()['GUEST_HOME']); globals()['CWD'] = globals()['GUEST_HOME']
    except: pass
    term.print("Logged out.", 0x00FFFF); update_prompt()

def cmd_scan(args):
    term.print("Scanning...", 0x00FFFF)
    try:
        nets = [n for n in wifi.radio.start_scanning_networks()]
        wifi.radio.stop_scanning_networks()
        for n in nets[:5]: term.print(f"{n.ssid} ({n.rssi})")
    except: term.print("Scan Failed", 0xFF0000)

def cmd_connect(args):
    """Usage: connect <ssid> [password]"""
    if not args: return term.print("Usage: connect <ssid> [pass]")
    ssid = args[0]
    pwd = None
    
    if len(args) > 1:
        pwd = args[1]
    elif ssid in SYSTEM_CONFIG["wifi"]:
        pwd = SYSTEM_CONFIG["wifi"][ssid]
        term.print(f"Found saved pass for {ssid}", 0x00FFFF)
    else:
        return term.print("Password required.", 0xFF0000)
        
    term.print(f"Connecting to {ssid}...", 0x00FFFF)
    try:
        wifi.radio.connect(ssid, pwd)
        term.print(f"IP: {wifi.radio.ipv4_address}", 0x00FF00)
        
        # Save if new or changed
        if ssid not in SYSTEM_CONFIG["wifi"] or SYSTEM_CONFIG["wifi"][ssid] != pwd:
            SYSTEM_CONFIG["wifi"][ssid] = pwd
            save_config(SYSTEM_CONFIG)
            term.print("Network Saved.", 0x00FF00)
            
    except Exception as e:
        term.print(f"Err: {e}", 0xFF0000)

def cmd_wget(args):
    if len(args) < 2: return term.print("wget <url> <file>")
    p = resolve_path(args[1])
    if not check_write_access(p): return
    try:
        url = args[0]; term.print(f"Get {url}...", 0x00FFFF)
        dummy, dummy, host, path = url.split("/", 3)
        pool = socketpool.SocketPool(wifi.radio)
        addr = pool.getaddrinfo(host, 80)[0][-1]
        s = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        s.connect(addr)
        s.send(f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        with open(p, "w") as f:
            while True:
                buf = s.recv(512)
                if not buf: break
                f.write(str(buf, 'utf-8'))
        s.close(); term.print("Done.")
    except Exception as e: term.print(f"Err: {e}", 0xFF0000)

COMMANDS = {
    "ls": cmd_ls, "cd": cmd_cd, "pwd": lambda x: term.print(globals()['CWD']),
    "cat": cmd_cat, "nano": cmd_nano, "rm": cmd_rm, "mkdir": cmd_mkdir,
    "cp": cmd_cp, "mv": cmd_mv, "touch": cmd_touch, "su": cmd_su,
    "login": cmd_su, "logout": cmd_logout, "whoami": lambda x: term.print(globals()['CURRENT_USER']),
    "scan": cmd_scan, "connect": cmd_connect, "wget": cmd_wget,
    "battery": cmd_battery, "bat": cmd_battery, "python": cmd_python,
    "disk": cmd_disk, "df": cmd_disk, "help": cmd_help, "time": cmd_time, "date": cmd_time,
    "ntp": cmd_ntp, "echo": cmd_echo, "sleep": cmd_sleep, "pbash": cmd_pbash,
    "passwd": cmd_passwd, "adduser": cmd_adduser,
    "ifconfig": lambda x: term.print(f"IP: {wifi.radio.ipv4_address}"),
    "clear": lambda x: term.clear(), "reboot": lambda x: microcontroller.reset(),
    "free": lambda x: term.print(f"RAM: {gc.mem_free()}")
}

# --- MAIN LOOP ---
def main_os():
    global kb, term, REPL_ENV, SYSTEM_CONFIG
    
    ROOT_HOME     = "/home/root"
    GUEST_HOME    = "/home/guest"
    SYSTEM_PATH   = ["/bin", "/sd/bin"]
    HIDDEN_FILES  = ["code.py", "boot.py", "lib", "config.json"]

    from cardputeradvkey import Keyboard
    from cardterm import Terminal
    kb = Keyboard()
    term = Terminal()
    term.boot_anim()

    REPL_ENV.update({
        "wifi": wifi, "os": os, "time": time, "sys": sys, 
        "term": term, "kb": kb, "board": board, 
        "displayio": displayio, "microcontroller": microcontroller,
        "analogio": analogio, "print": virtual_print, "input": virtual_input
    })
    
    SHELL_HISTORY = []
    HIST_IDX = 0
    CURRENT_USER = "guest"
    
    globals()['CURRENT_USER'] = CURRENT_USER
    globals()['CWD'] = "/"
    globals()['PROMPT_CHAR'] = "$"
    globals()['ROOT_HOME'] = ROOT_HOME
    globals()['GUEST_HOME'] = GUEST_HOME
    globals()['SYSTEM_PATH'] = SYSTEM_PATH
    globals()['HIDDEN_FILES'] = HIDDEN_FILES

    # --- LOAD CONFIG ---
    SYSTEM_CONFIG = load_config()
    if "root" not in SYSTEM_CONFIG["users"]: SYSTEM_CONFIG["users"]["root"] = "cardos"
    if "guest" not in SYSTEM_CONFIG["users"]: SYSTEM_CONFIG["users"]["guest"] = ""
    # -------------------

    try:
        os.listdir("/")
        try: os.stat("/home"); 
        except: 
            try: os.mkdir("/home")
            except: pass
        try: os.stat(GUEST_HOME)
        except: 
            try: os.mkdir(GUEST_HOME)
            except: pass
        try: os.stat(ROOT_HOME)
        except: 
            try: os.mkdir(ROOT_HOME)
            except: pass
    except: term.print("[INIT] Drive Err", 0x555555)

    try:
        os.stat(GUEST_HOME); globals()['CWD'] = GUEST_HOME
    except: globals()['CWD'] = "/"

    # AUTO-BOOT SCRIPT
    if file_exists("/boot.pbash"):
        term.print("Booting (Internal)...", 0x00FF00)
        run_script_file("/boot.pbash")
    elif file_exists("/sd/boot.pbash"):
        term.print("Booting (SD)...", 0x00FF00)
        run_script_file("/sd/boot.pbash")

    update_prompt()

    globals()['current_input'] = ""
    cursor_pos = 0

    while True:
        char = kb.check()
        if char:
            if char == "ENTER":
                term.print(f"{globals()['PROMPT_CHAR']} {globals()['current_input']}", 0x555555)
                if globals()['current_input']:
                    SHELL_HISTORY.append(globals()['current_input'])
                    HIST_IDX = len(SHELL_HISTORY)

                run_command_line(globals()['current_input'])
                
                globals()['current_input'] = ""
                cursor_pos = 0
                term.label_input.text = "_"
                
            elif char == "UP":
                if HIST_IDX > 0:
                    HIST_IDX -= 1; globals()['current_input'] = SHELL_HISTORY[HIST_IDX]
                    cursor_pos = len(globals()['current_input'])
            elif char == "DOWN":
                if HIST_IDX < len(SHELL_HISTORY) - 1:
                    HIST_IDX += 1; globals()['current_input'] = SHELL_HISTORY[HIST_IDX]
                    cursor_pos = len(globals()['current_input'])
                else: globals()['current_input'] = ""; HIST_IDX = len(SHELL_HISTORY); cursor_pos = 0
            elif char == "LEFT":
                if cursor_pos > 0: cursor_pos -= 1
            elif char == "RIGHT":
                if cursor_pos < len(globals()['current_input']): cursor_pos += 1
            elif char == "DEL": 
                if cursor_pos > 0:
                    c = globals()['current_input']
                    globals()['current_input'] = c[:cursor_pos-1] + c[cursor_pos:]
                    cursor_pos -= 1
            elif char == "TAB":
                completed = tab_complete(globals()['current_input'])
                globals()['current_input'] = completed
                cursor_pos = len(completed)
            elif len(char) == 1 or char == "SPACE":
                ins = " " if char == "SPACE" else char
                c = globals()['current_input']
                globals()['current_input'] = c[:cursor_pos] + ins + c[cursor_pos:]
                cursor_pos += 1

            vis_str = globals()['current_input'][:cursor_pos] + "_" + globals()['current_input'][cursor_pos:]
            if len(vis_str) > 28:
                start = max(0, cursor_pos - 14)
                if start + 28 > len(vis_str): start = max(0, len(vis_str) - 28)
                disp = vis_str[start : start+28]
                if start > 0: disp = "." + disp[1:]
                term.label_input.text = disp
            else: term.label_input.text = vis_str
            
        time.sleep(0.005)

globals()['current_input'] = ""
try:
    main_os()
except Exception as e:
    recovery_mode(e)