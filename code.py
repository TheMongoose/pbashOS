import sys
import os
import time
import wifi
import socketpool
import ipaddress
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
import digitalio
import alarm

# --- GLOBAL VARS ---
kb = None
term = None
REPL_ENV = {}
SYSTEM_CONFIG = {}
SD_HARDWARE = {"spi": None, "cs": None, "sd": None, "vfs": None}

# --- SETTINGS ---
HIDDEN_FILES = ["code.py", "boot.py", "lib", "config.json", "System Volume Information"]
PROTECTED_PATHS = ["/code.py", "/boot.py", "/lib", "/config.json"]

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

def check_access(path, write_mode=False):
    """Central Security Check"""
    user = globals()['CURRENT_USER']
    if user == "root": return True
    
    # 1. System File Protection
    for protected in PROTECTED_PATHS:
        if path == protected or path.startswith(protected + "/"):
            term.print("Permission Denied (System)", 0xFF0000)
            return False
            
    # 2. Write Protection
    if write_mode:
        if path.startswith(globals()['GUEST_HOME']): return True
        if path.startswith("/sd"): return True
        term.print("Permission Denied (Write)", 0xFF0000)
        return False
    return True

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
        if not check_access(exec_path): return
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
    if not check_access(path): return
    try:
        with open(path, "r") as f:
            lines = f.readlines()
            for line in lines:
                l = line.strip()
                if l: run_command_line(l)
    except Exception as e:
        term.print(f"Script Err: {e}", 0xFF0000)

# --- HARDWARE MANAGERS ---
def mount_sd_card(verbose=False):
    global SD_HARDWARE
    if verbose: term.print("Init SPI (40,14,39)...", 0x00FFFF)
    try:
        if SD_HARDWARE["vfs"]:
            try:
                os.stat("/sd")
                if verbose: term.print("Already mounted.", 0x00FF00)
                return True
            except: SD_HARDWARE["vfs"] = None
        import adafruit_sdcard
        if not SD_HARDWARE["spi"]: 
            SD_HARDWARE["spi"] = busio.SPI(board.IO40, board.IO14, board.IO39)
        if not SD_HARDWARE["cs"]: 
            SD_HARDWARE["cs"] = digitalio.DigitalInOut(board.IO12)
        SD_HARDWARE["sd"] = adafruit_sdcard.SDCard(SD_HARDWARE["spi"], SD_HARDWARE["cs"], baudrate=4000000)
        SD_HARDWARE["vfs"] = storage.VfsFat(SD_HARDWARE["sd"])
        storage.mount(SD_HARDWARE["vfs"], "/sd")
        if verbose: term.print("Success!", 0x00FF00)
        else: term.print("[INIT] SD Mounted", 0x00FF00)
        return True
    except Exception as e:
        if verbose: term.print(f"Err: {e}", 0xFF0000)
        else: term.print(f"[INIT] No SD: {e}", 0x555555)
    return False

def unmount_sd_card(verbose=False):
    global SD_HARDWARE
    try:
        storage.umount("/sd")
        SD_HARDWARE["vfs"] = None
        SD_HARDWARE["sd"] = None
        if verbose: term.print("Unmounted.", 0x00FFFF)
    except Exception as e:
        if verbose: term.print(f"Unmount Err: {e}", 0xFF0000)

def load_config():
    # CHANGED DEFAULT PASSWORD HERE
    default_config = { "users": {"root": "pbash", "guest": ""}, "wifi": {} }
    try:
        if file_exists("/config.json"):
            with open("/config.json", "r") as f:
                conf = json.load(f)
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
    except OSError: return False

# --- COMMAND DEFINITIONS ---
def cmd_ls(args):
    """ls [-a] [path]"""
    show_all = False
    path_arg = None
    for arg in args:
        if arg == "-a" or arg == "all": show_all = True
        elif not arg.startswith("-"): path_arg = arg
    
    target = resolve_path(path_arg if path_arg else globals()['CWD'])
    is_guest = globals()['CURRENT_USER'] == "guest"
    
    try:
        items = os.listdir(target)
        dirs = []
        files = []
        for item in sorted(items):
            if not show_all and item.startswith("."): continue
            
            if is_guest:
                full_check = target + "/" + item if target != "/" else "/" + item
                if full_check in PROTECTED_PATHS: continue
            
            if not is_guest and not show_all and target == "/" and "/" + item in PROTECTED_PATHS:
                continue

            full = target + "/" + item if target != "/" else "/" + item
            try:
                if os.stat(full)[0] & 0x4000: dirs.append(item + "/")
                else: files.append(item)
            except: files.append(item)
        if dirs: term.print("  ".join(dirs), 0x00FFFF)
        if files: term.print("  ".join(files), 0x00FF00)
        if not dirs and not files: term.print("(empty)", 0x555555)
    except OSError: term.print(f"Err {target}", 0xFF0000)

def cmd_cd(args):
    """Change Directory"""
    target = globals()['GUEST_HOME'] if globals()['CURRENT_USER'] == "guest" else globals()['ROOT_HOME']
    if args: target = resolve_path(args[0])
    
    if not check_access(target): return
    
    try: 
        os.listdir(target)
        globals()['CWD'] = target
    except: 
        term.print("Invalid dir", 0xFF0000)
    update_prompt()

def cmd_cat(args):
    if not args: return
    p = resolve_path(args[0])
    if not check_access(p): return
    try:
        with open(p, "r") as f: term.print(f.read())
    except: term.print("Read Error", 0xFF0000)

def cmd_rm(args):
    """rm <file> OR rm dir <folder>"""
    if not args: return
    recursive = "dir" in args
    targets = [a for a in args if a != "dir"]
    if not targets: return
    
    p = resolve_path(targets[0])
    if not check_access(p, write_mode=True): return
    
    def r_rm(path):
        if is_dir(path):
            for c in os.listdir(path): r_rm(path + "/" + c)
            try: os.rmdir(path)
            except: pass
        else: os.remove(path)
    
    try:
        if is_dir(p):
            if recursive: 
                term.print("Deleting...", 0xFFA500)
                r_rm(p)
                term.print("Done.")
            else: 
                term.print("Use 'rm dir <name>'", 0xFF0000)
        else: 
            os.remove(p)
            term.print("Deleted file")
    except Exception as e: term.print(f"Fail: {e}", 0xFF0000)

def cmd_mkdir(args):
    if not args: return
    p = resolve_path(args[0])
    if not check_access(p, write_mode=True): return
    try: os.mkdir(p); term.print("Created")
    except: term.print("Fail", 0xFF0000)

def cmd_cp(args):
    if len(args) < 2: return
    src, dst = resolve_path(args[0]), resolve_path(args[1])
    if not check_access(src): return
    if not check_access(dst, write_mode=True): return
    try:
        with open(src, "r") as s, open(dst, "w") as d: d.write(s.read())
        term.print("Copied")
    except: term.print("Err", 0xFF0000)

def cmd_mv(args):
    if len(args) < 2: return
    src, dst = resolve_path(args[0]), resolve_path(args[1])
    if not check_access(src, write_mode=True): return
    if not check_access(dst, write_mode=True): return
    try: os.rename(src, dst); term.print("Moved")
    except: term.print("Err", 0xFF0000)

def cmd_touch(args):
    if not args: return
    p = resolve_path(args[0])
    if not check_access(p, write_mode=True): return
    try: open(p, "a").close(); term.print("Touched")
    except: term.print("Err", 0xFF0000)

def cmd_nano(args):
    if not args: return term.print("Usage: nano <file>")
    fname = resolve_path(args[0])
    if not check_access(fname): return
    can_write = check_access(fname, write_mode=True)
    lines = [""]
    try:
        with open(fname, "r") as f: lines = f.read().split("\n")
    except: pass
    cx, cy, sy = 0, 0, 0
    term.label_prompt.text = ""
    while True:
        disp = ""
        for i, l in enumerate(lines[sy:sy+9]):
            disp += (">" if sy+i == cy else " ") + l[:38] + "\n"
        term.label_console.text = disp
        status = f"CTRL:Save ESC:Exit" if can_write else "[RO] ESC:Exit"
        term.label_input.text = f"{status} | {cy+1}:{cx}"
        c = kb.check()
        if c:
            if c == "ESCAPE": break
            elif c == "UP": 
                if cy > 0: cy -= 1
                if cy < sy: sy -= 1
            elif c == "DOWN":
                if cy < len(lines)-1: cy += 1
                if cy >= sy+9: sy += 1
            elif c == "LEFT" and cx > 0: cx -= 1
            elif c == "RIGHT" and cx < len(lines[cy]): cx += 1
            elif can_write:
                if c == "ENTER":
                    lines.insert(cy+1, lines[cy][cx:]); lines[cy] = lines[cy][:cx]; cy += 1; cx = 0
                elif c == "DEL":
                    if cx > 0: lines[cy] = lines[cy][:cx-1] + lines[cy][cx:]; cx -= 1
                    elif cy > 0: cx = len(lines[cy-1]); lines[cy-1] += lines[cy]; del lines[cy]; cy -= 1
                elif c == "CTRL":
                    try: 
                        with open(fname, "w") as f: f.write("\n".join(lines))
                        term.label_input.text = "SAVED"; time.sleep(0.5)
                    except: term.label_input.text = "ERR: Save Fail"
                elif len(c) == 1 or c == "SPACE":
                    ch = " " if c == "SPACE" else c
                    lines[cy] = lines[cy][:cx] + ch + lines[cy][cx:]; cx += 1
        time.sleep(0.01)
    term.clear(); update_prompt()

def cmd_shutdown(args):
    term.print("Shutting down...", 0xFFA500)
    time.sleep(1)
    # Go into deep sleep (Wake on Reset)
    alarm_obj = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + 31536000)
    alarm.exit_and_deep_sleep_until_alarms(alarm_obj)

def cmd_storage(args):
    if not args:
        term.print("Storage Manager:", 0x00FFFF)
        term.print("  storage status  - Check stats")
        term.print("  storage mount   - Force Mount")
        term.print("  storage unmount - Safe remove")
        term.print("  storage test    - R/W Test")
        return
    sub = args[0]
    if sub == "mount": mount_sd_card(verbose=True)
    elif sub == "unmount": unmount_sd_card(verbose=True)
    elif sub == "status":
        try:
            s = os.statvfs("/sd")
            bs = s[0]; total = (s[2] * bs) / 1024 / 1024; free = (s[3] * bs) / 1024 / 1024
            term.print("SD Card Status:", 0x00FF00)
            term.print(f"  Total: {total:.1f} MB")
            term.print(f"  Free:  {free:.1f} MB")
        except: term.print("SD not accessible.", 0xFF0000)
    elif sub == "test":
        try:
            with open("/sd/test", "w") as f: f.write("OK")
            os.remove("/sd/test")
            term.print("SD IO OK", 0x00FF00)
        except Exception as e: term.print(f"Err: {e}", 0xFF0000)

def cmd_echo(args): term.print(" ".join(args))
def cmd_sleep(args): 
    if args: time.sleep(float(args[0]))
def cmd_pbash(args): 
    if args: run_script_file(args[0])
    else: term.print("Usage: pbash <file>")

def cmd_ping(args):
    if not args: return term.print("Usage: ping <host>")
    if not wifi.radio.ipv4_address: return term.print("No WiFi connected.", 0xFF0000)
    try:
        pool = socketpool.SocketPool(wifi.radio)
        ip = ipaddress.ip_address(pool.getaddrinfo(args[0], 80)[0][4][0])
        term.print(f"Pinging {ip}...", 0x00FFFF)
        for i in range(4):
            t = wifi.radio.ping(ip)
            if t: term.print(f"Reply: time={t*1000:.1f}ms", 0x00FF00)
            else: term.print("Timeout", 0xFFA500)
            time.sleep(0.5)
    except: term.print("Ping Fail", 0xFF0000)

def cmd_ntp(args):
    if not wifi.radio.ipv4_address: return term.print("No WiFi", 0xFF0000)
    offset = int(args[0]) if args else 0
    term.print(f"Syncing (UTC{offset:+})...", 0x00FFFF)
    try:
        pool = socketpool.SocketPool(wifi.radio)
        packet = bytearray(48); packet[0] = 0x1B
        with pool.socket(pool.AF_INET, pool.SOCK_DGRAM) as sock:
            sock.settimeout(5)
            sock.sendto(packet, ("pool.ntp.org", 123))
            size, addr = sock.recvfrom_into(packet)
            t = struct.unpack("!I", packet[40:44])[0] - 2208988800 + (offset * 3600)
            rtc.RTC().datetime = time.localtime(t)
            term.print("Time Set!", 0x00FF00)
            cmd_time([])
    except: term.print("NTP Fail", 0xFF0000)

def cmd_time(args):
    t = time.localtime()
    term.print("{:02}:{:02}:{:02} ({}/{}/{})".format(t.tm_hour, t.tm_min, t.tm_sec, t.tm_mon, t.tm_mday, t.tm_year), 0x00FFFF)

def cmd_help(args):
    term.print("Available Commands:", 0x00FFFF)
    term.print(" ".join(sorted(COMMANDS.keys())))

def cmd_disk(args):
    try:
        s = os.statvfs(globals()['CWD'])
        bs = s[0]; tot = s[2]*bs; free = s[3]*bs
        term.print(f"Path: {globals()['CWD']}", 0x00FFFF)
        term.print(f"Total: {tot/1024/1024:.1f} MB")
        term.print(f"Free:  {free/1024/1024:.1f} MB", 0x00FF00)
    except: term.print("Err", 0xFF0000)

def cmd_python(args):
    term.print("REPL (ESC exit)", 0x00FF00)
    old = term.label_prompt.text
    term.label_prompt.text = ">>> "
    cmd = ""
    while True:
        c = kb.check()
        if c:
            if c == "ESCAPE": break
            elif c == "ENTER":
                term.print(f">>> {cmd}", 0x555555)
                try: exec(cmd, REPL_ENV)
                except Exception as e: term.print(f"{e}", 0xFF0000)
                cmd = ""; term.label_input.text = "_"
            elif c == "DEL": cmd = cmd[:-1]
            elif c == "SPACE": cmd += " "
            elif len(c) == 1: cmd += c
            term.label_input.text = cmd + "_"
        time.sleep(0.01)
    term.label_prompt.text = old
    term.print("Exited.")

def cmd_battery(args):
    adc = analogio.AnalogIn(board.IO10)
    v = (adc.value * 3.3 / 65535) * 2
    p = max(0, min(100, (v - 3.2) / (4.2 - 3.2) * 100))
    term.print(f"Bat: {p:.0f}% ({v:.2f}V)", 0x00FF00)
    adc.deinit()

def cmd_su(args):
    target = args[0] if args else "root"
    if target not in SYSTEM_CONFIG["users"]: return term.print("No user", 0xFF0000)
    term.print("Pass:", 0xFFFF00); p=""
    while True:
        c=kb.check()
        if c:
            if c=="ENTER": break
            elif c=="DEL": p=p[:-1]
            elif len(c)==1: p+=c
            term.label_input.text = "*"*len(p)+"_"
        time.sleep(0.01)
    if p==SYSTEM_CONFIG["users"][target]:
        globals()['CURRENT_USER']=target
        h = f"/home/{target}" if target not in ["root", "guest"] else (globals()['ROOT_HOME'] if target=="root" else globals()['GUEST_HOME'])
        try: os.stat(h); globals()['CWD'] = h
        except: pass
        term.print("OK", 0x00FF00)
    else: term.print("Fail", 0xFF0000)
    update_prompt()

def cmd_adduser(args):
    if globals()['CURRENT_USER']!="root" or not args: return
    u = args[0]; SYSTEM_CONFIG["users"][u]="1234"
    save_config(SYSTEM_CONFIG)
    try: os.mkdir(f"/home/{u}")
    except: pass
    term.print(f"Added {u} (pass: 1234)")

def cmd_passwd(args):
    term.print("New Pass:", 0xFFFF00); p=""
    while True:
        c=kb.check()
        if c:
            if c=="ENTER": break
            elif c=="DEL": p=p[:-1]
            elif len(c)==1: p+=c
            term.label_input.text = "*"*len(p)+"_"
        time.sleep(0.01)
    SYSTEM_CONFIG["users"][globals()['CURRENT_USER']]=p
    save_config(SYSTEM_CONFIG); term.print("Saved")

def cmd_logout(args):
    globals()['CURRENT_USER']="guest"; update_prompt()

def cmd_scan(args):
    for n in wifi.radio.start_scanning_networks(): term.print(f"{n.ssid} {n.rssi}")
    wifi.radio.stop_scanning_networks()

def cmd_connect(args):
    if not args: return
    p = args[1] if len(args)>1 else SYSTEM_CONFIG["wifi"].get(args[0])
    if p:
        try: 
            wifi.radio.connect(args[0], p); term.print("Connected", 0x00FF00)
            SYSTEM_CONFIG["wifi"][args[0]]=p; save_config(SYSTEM_CONFIG)
        except: term.print("Fail", 0xFF0000)
    else: term.print("Pass required", 0xFF0000)

def cmd_wget(args):
    if len(args)<2: return
    try:
        pool = socketpool.SocketPool(wifi.radio)
        r = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        r.connect(pool.getaddrinfo(args[0].split("/")[2], 80)[0][-1])
        r.send(f"GET /{''.join(args[0].split('/')[3:])} HTTP/1.0\r\n\r\n".encode())
        with open(resolve_path(args[1]), "w") as f:
            while True:
                d = r.recv(128)
                if not d: break
                f.write(str(d, 'utf-8'))
        term.print("Done")
    except: term.print("Fail", 0xFF0000)

# --- COMMAND REGISTRY ---
COMMANDS = {
    "ls": cmd_ls,
    "cd": cmd_cd,
    "pwd": lambda x: term.print(globals()['CWD']),
    "cat": cmd_cat,
    "nano": cmd_nano,
    "rm": cmd_rm,
    "mkdir": cmd_mkdir,
    "cp": cmd_cp,
    "mv": cmd_mv,
    "touch": cmd_touch,
    "su": cmd_su,
    "login": cmd_su,
    "logout": cmd_logout,
    "whoami": lambda x: term.print(globals()['CURRENT_USER']),
    "scan": cmd_scan,
    "connect": cmd_connect,
    "wget": cmd_wget,
    "battery": cmd_battery,
    "bat": cmd_battery,
    "python": cmd_python,
    "disk": cmd_disk,
    "df": cmd_disk,
    "help": cmd_help,
    "time": cmd_time,
    "date": cmd_time,
    "ntp": cmd_ntp,
    "echo": cmd_echo,
    "sleep": cmd_sleep,
    "pbash": cmd_pbash,
    "ping": cmd_ping,
    "passwd": cmd_passwd,
    "adduser": cmd_adduser,
    "storage": cmd_storage,
    "shutdown": cmd_shutdown,
    "ifconfig": lambda x: term.print(f"IP: {wifi.radio.ipv4_address}"),
    "clear": lambda x: term.clear(),
    "reboot": lambda x: microcontroller.reset(),
    "free": lambda x: term.print(f"RAM: {gc.mem_free()}")
}

# --- MAIN LOOP ---
def main_os():
    global kb, term, REPL_ENV, SYSTEM_CONFIG
    
    # OS Config
    ROOT_HOME     = "/home/root"
    GUEST_HOME    = "/home/guest"
    SYSTEM_PATH   = ["/bin", "/sd/bin"]
    
    # Init Hardware
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

    # Init Subsystems
    mount_sd_card()
    SYSTEM_CONFIG = load_config()
    
    # Defaults
    if "root" not in SYSTEM_CONFIG["users"]: SYSTEM_CONFIG["users"]["root"] = "pbash"
    if "guest" not in SYSTEM_CONFIG["users"]: SYSTEM_CONFIG["users"]["guest"] = ""

    # Create Dirs
    try:
        os.listdir("/")
        for d in ["/home", GUEST_HOME, ROOT_HOME]:
            try: os.mkdir(d)
            except: pass
    except: term.print("[INIT] Drive Err", 0x555555)

    try: os.stat(GUEST_HOME); globals()['CWD'] = GUEST_HOME
    except: globals()['CWD'] = "/"

    # Boot Scripts
    if file_exists("/boot.pbash"): run_script_file("/boot.pbash")
    elif file_exists("/sd/boot.pbash"): run_script_file("/sd/boot.pbash")

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
            # Scrolling input display
            if len(vis_str) > 28:
                start = max(0, cursor_pos - 14)
                if start + 28 > len(vis_str): start = max(0, len(vis_str) - 28)
                disp = vis_str[start : start+28]
                if start > 0: disp = "." + disp[1:]
                term.label_input.text = disp
            else:
                term.label_input.text = vis_str
            
        time.sleep(0.005)

globals()['current_input'] = ""
try:
    main_os()
except Exception as e:
    recovery_mode(e)