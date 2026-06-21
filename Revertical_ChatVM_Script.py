import pytchat
import subprocess
import pathlib
import sys
import time
import os
import shutil
import threading
from collections import deque
import tkinter as tk
from tkinter import ttk, messagebox

def find_vbox_path():
    path = shutil.which("VBoxManage")
    if path: return pathlib.Path(path)
    win_paths = [
        pathlib.Path(os.environ.get("VBOX_MSI_INSTALL_PATH", "")) / "VBoxManage.exe",
        pathlib.Path(r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"),
        pathlib.Path(r"C:\Program Files (x86)\Oracle\VirtualBox\VBoxManage.exe")
    ]
    for p in win_paths:
        if p.exists(): return p
    return None

VBOX_PATH = find_vbox_path()

VM_NAME = "VirtualMachineName"
VIDEO_ID = "insert_id_here"
SNAPSHOT_NAME = "put_snapshot_name_here"
AUTHORIZED_USERS = {"@InsertAuthorizedUsers"}

VM_STABILIZE_DELAY = 0.02  
MAX_WAIT = 5
VOTE_EXPIRY = 60 
MAX_QUEUE_SIZE = 100
VOTES_REQUIRED = 2

command_queue = deque(maxlen=MAX_QUEUE_SIZE)
queue_lock = threading.Lock()  
cursor_x, cursor_y = 16384, 16384 

SCANCODES = {
    **{chr(i): hex(code)[2:] for i, code in zip(range(97, 123), [
        0x1e,0x30,0x2e,0x20,0x12,0x21,0x22,0x23,0x17,0x24,
        0x25,0x26,0x32,0x31,0x18,0x19,0x10,0x13,0x1f,0x14,
        0x16,0x2f,0x11,0x2d,0x15,0x2c
    ])},
    **{str(i): format(code, '02x') for i, code in enumerate([0x0b,0x02,0x03,0x04,0x05,0x06,0x07,0x08,0x09,0x0a])},
    **{f"f{i}": hex(0x3a + i)[2:] for i in range(1, 11)},
    "f11": "57", "f12": "58",
    "enter":"1c","esc":"01","backspace":"0e","tab":"0f","space":"39",
    "ins":"e0 52","del":"e0 53","home":"e0 47","end":"e0 4f",
    "pgup":"e0 49","pgdn":"e0 51","up":"e0 48","down":"e0 50",
    "left":"e0 4b","right":"e0 4d","shift":"2a","ctrl":"1d",
    "alt":"38","win":"e0 5b","cmd":"e0 5b","caps":"3a",
    "-":"0c","=":"0d","[":"1a","]":"1b",";":"27","'":"28",
    "`":"29","\\":"2b",",":"33",".":"34","/":"35",
}

SHIFTED_CHARS = {
    '~': '`', '!': '1', '@': '2', '#': '3', '$': '4', '%': '5',
    '^': '6', '&': '7', '*': '8', '(': '9', ')': '0', '_': '-',
    '+': '=', '{': '[', '}': ']', '|': '\\', ':': ';', '"': "'",
    '<': ',', '>': '.', '?': '/'
}

class VoteManager:
    def __init__(self):
        self.registry = {"restartvm": {}, "shutdown": {}, "forceshutdown": {}, "revert": {}}

    def _cleanup(self, command):
        now = time.time()
        expired = [u for u, t in self.registry[command].items() if now - t > VOTE_EXPIRY]
        for u in expired: del self.registry[command][u]

    def check_vote(self, user, command, is_privileged):
        if is_privileged:
            self.registry[command].clear()
            return True
        self._cleanup(command)
        self.registry[command][user] = time.time()
        count = len(self.registry[command])
        print(f"[Vote] {user} for {command} ({count}/{VOTES_REQUIRED})")
        if count >= VOTES_REQUIRED:
            self.registry[command].clear()
            return True
        return False

vote_manager = VoteManager()

def get_vm_state():
    try:
        res = subprocess.run([str(VBOX_PATH), "showvminfo", VM_NAME, "--machinereadable"], capture_output=True, text=True, timeout=1)
        for line in res.stdout.splitlines():
            if line.startswith("VMState="): return line.split("=")[1].strip('"')
    except: pass
    return "unknown"

def send_vbox_command_sync(sub_cmd, *args):
    full_args = [str(VBOX_PATH), "controlvm", VM_NAME, sub_cmd] + list(args)
    subprocess.run(full_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def type_text(user, text):
    full_batch = []
    shift_press = ["2a"]
    shift_release = ["aa"]

    for char in text:
        use_shift = False
        lookup_key = char

        if char == " ": lookup_key = "space"
        elif char in SHIFTED_CHARS:
            use_shift = True
            lookup_key = SHIFTED_CHARS[char]
        elif char.isupper() and char.lower() in SCANCODES:
            use_shift = True
            lookup_key = char.lower()

        if lookup_key in SCANCODES:
            parts = SCANCODES[lookup_key].split()
            release = [p if p == "e0" else format(int(p, 16) + 0x80, "x") for p in parts]
            if use_shift:
                full_batch.extend(shift_press + parts + release + shift_release)
            else:
                full_batch.extend(parts + release)
    
    if full_batch:
        send_vbox_command_sync("keyboardputscancode", *full_batch)

def revert_vm(user):
    print(f"[Revert] {user} initiated revert")
    subprocess.run([str(VBOX_PATH), "controlvm", VM_NAME, "poweroff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if get_vm_state() == "poweroff": break
        time.sleep(0.2)
    time.sleep(1.0) 
    subprocess.run([str(VBOX_PATH), "snapshot", VM_NAME, "restore", SNAPSHOT_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    subprocess.run([str(VBOX_PATH), "startvm", VM_NAME, "--type", "gui"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def run_combo(user, keys):
    parts = [k.strip().lower() for k in keys.split("+")]
    mods = [k for k in parts if k in ["ctrl", "shift", "alt", "win", "cmd"]]
    main = parts[-1]
    
    batch = []
    for m in mods:
        if m in SCANCODES: batch.extend(SCANCODES[m].split())
    if main in SCANCODES:
        main_parts = SCANCODES[main].split()
        batch.extend(main_parts)
        batch.extend([p if p == "e0" else format(int(p, 16) + 0x80, "x") for p in main_parts])
    for m in reversed(mods):
        if m in SCANCODES:
            m_parts = SCANCODES[m].split()
            batch.extend([p if p == "e0" else format(int(p, 16) + 0x80, "x") for p in m_parts])
            
    send_vbox_command_sync("keyboardputscancode", *batch)

def move_mouse(user, direction, amount):
    global cursor_x, cursor_y
    try: dist = int(amount)
    except: dist = 1000
    dir = direction.lower()
    if dir == "up": cursor_y = max(0, cursor_y - dist)
    elif dir == "down": cursor_y = min(32767, cursor_y + dist)
    elif dir == "left": cursor_x = max(0, cursor_x - dist)
    elif dir == "right": cursor_x = min(32767, cursor_x + dist)
    send_vbox_command_sync("mouseputabsolute", str(cursor_x), str(cursor_y), "0")

def mouse_click(user, button):
    code = {"left":"1", "right":"2", "middle":"4"}.get(button.lower(), "1")
    send_vbox_command_sync("mouseputabsolute", str(cursor_x), str(cursor_y), code)
    time.sleep(0.02)
    send_vbox_command_sync("mouseputabsolute", str(cursor_x), str(cursor_y), "0")

def parse_and_queue(user, raw_cmd, is_priv):
    parts = raw_cmd.strip().split(maxsplit=1)
    if not parts: return
    cmd, arg = parts[0].lower(), parts[1] if len(parts) > 1 else ""

    with queue_lock:
        if cmd == "revert" and vote_manager.check_vote(user, "revert", is_priv):
            command_queue.append((revert_vm, (user,)))
        elif cmd == "restartvm" and vote_manager.check_vote(user, "restartvm", is_priv):
            command_queue.append((lambda u: send_vbox_command_sync("reset"), (user,)))
        elif cmd == "shutdown" and vote_manager.check_vote(user, "shutdown", is_priv):
            command_queue.append((lambda u: send_vbox_command_sync("acpipowerbutton"), (user,)))
        elif cmd == "forceshutdown" and vote_manager.check_vote(user, "forceshutdown", is_priv):
            command_queue.append((lambda u: send_vbox_command_sync("poweroff"), (user,)))
        elif cmd == "move" and arg:
            m_parts = arg.split()
            if len(m_parts) >= 2: command_queue.append((move_mouse, (user, m_parts[0], m_parts[1])))
        elif cmd in ["click", "rclick", "mclick"]:
            btn = "right" if cmd == "rclick" else "middle" if cmd == "mclick" else "left"
            command_queue.append((mouse_click, (user, btn)))
        elif cmd == "startvm":
            command_queue.append((lambda u: subprocess.run([str(VBOX_PATH), "startvm", VM_NAME, "--type", "gui"]), (user,)))
        elif cmd == "run" and arg:
            command_queue.append((run_combo, (user, "win+r")))
            command_queue.append((time.sleep, (0.15,)))
            command_queue.append((type_text, (user, arg)))
            command_queue.append((time.sleep, (0.05,)))
            command_queue.append((run_combo, (user, "enter")))
        elif cmd == "type" and arg: command_queue.append((type_text, (user, arg)))
        elif cmd == "send" and arg:
            command_queue.append((type_text, (user, arg)))
            command_queue.append((time.sleep, (0.05,)))
            command_queue.append((run_combo, (user, "enter")))
        elif cmd == "key" and arg:
            key_val = arg.lower()
            if key_val in SCANCODES: command_queue.append((run_combo, (user, key_val)))
        elif cmd == "combo" and arg:
            command_queue.append((run_combo, (user, arg)))
        elif cmd == "wait" and arg:
            try: command_queue.append((time.sleep, (min(float(arg), MAX_WAIT),)))
            except: pass

def run_vbox_engine():
    global VM_NAME, VIDEO_ID, SNAPSHOT_NAME, AUTHORIZED_USERS
    
    print(f"[init] Initializing...")
    if get_vm_state() not in ["running", "paused"]:
        print("Auto starting VM on script start...")
        subprocess.run([str(VBOX_PATH), "startvm", VM_NAME, "--type", "gui"])
        time.sleep(2)
        
    chat = pytchat.create(video_id=VIDEO_ID, interruptable=False)
    while chat.is_alive():
        try:
            for c in chat.get().sync_items():
                print(f"[{c.author.name}]: {c.message}")
                is_priv = c.author.isChatModerator or c.author.isChatOwner or c.author.name in AUTHORIZED_USERS
                for seg in c.message.split("!"):
                    if seg.strip(): parse_and_queue(c.author.name, seg, is_priv)
            
            has_items = True
            while has_items:
                func, args = None, None
                with queue_lock:
                    if command_queue: func, args = command_queue.popleft()
                    else: has_items = False
                if func:
                    func(*args)
                    if VM_STABILIZE_DELAY > 0: time.sleep(VM_STABILIZE_DELAY)
            time.sleep(0.01)
        except Exception as e:
            print(f"An error has occured :( Reason: {e}")
            time.sleep(1)

class ControlGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("revertical's script version 0.1")
        self.root.geometry("460x320")
        self.root.resizable(False, False)
        
        frame = ttk.Frame(root, padding="20")
        frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        if not VBOX_PATH:
            messagebox.showerror("Ugh!", "I can't find VBoxManage, install VirtualBox please")
            sys.exit(1)
            
        ttk.Label(frame, text="Virtual Machine Name:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.ent_vm = ttk.Entry(frame, width=35)
        self.ent_vm.insert(0, VM_NAME)
        self.ent_vm.grid(row=0, column=1, pady=5, padx=5)

        ttk.Label(frame, text="YouTube live stream ID:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.ent_video = ttk.Entry(frame, width=35)
        self.ent_video.insert(0, VIDEO_ID)
        self.ent_video.grid(row=1, column=1, pady=5, padx=5)

        ttk.Label(frame, text="Snapshot name:").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.ent_snap = ttk.Entry(frame, width=35)
        self.ent_snap.insert(0, SNAPSHOT_NAME)
        self.ent_snap.grid(row=2, column=1, pady=5, padx=5)

        ttk.Label(frame, text="whitelisted users:\n(separated by a comma)").grid(row=3, column=0, sticky=tk.W, pady=5)
        self.ent_users = ttk.Entry(frame, width=35)
        self.ent_users.insert(0, ", ".join(AUTHORIZED_USERS))
        self.ent_users.grid(row=3, column=1, pady=5, padx=5)
        
        self.btn_toggle = ttk.Button(frame, text="Start the chaos", command=self.start_controller, width=25)
        self.btn_toggle.grid(row=4, column=0, columnspan=2, pady=25)
        
        self.lbl_status = ttk.Label(frame, text="status: not started", font=("Arial", 10, "italic"), foreground="blue")
        self.lbl_status.grid(row=5, column=0, columnspan=2, pady=2)

    def start_controller(self):
        global VM_NAME, VIDEO_ID, SNAPSHOT_NAME, AUTHORIZED_USERS
        
        VM_NAME = self.ent_vm.get().strip()
        VIDEO_ID = self.ent_video.get().strip()
        SNAPSHOT_NAME = self.ent_snap.get().strip()
        AUTHORIZED_USERS = {u.strip() for u in self.ent_users.get().split(",") if u.strip()}
        
        if not VM_NAME or not VIDEO_ID or not SNAPSHOT_NAME:
            messagebox.showwarning("you forgot something", "please fill in all information")
            return

        self.ent_vm.config(state="disabled")
        self.ent_video.config(state="disabled")
        self.ent_snap.config(state="disabled")
        self.ent_users.config(state="disabled")
        self.btn_toggle.config(state="disabled")
        
        self.lbl_status.config(text="status: running", foreground="green")
        
        worker = threading.Thread(target=run_vbox_engine, daemon=True)
        worker.start()

if __name__ == "__main__":
    app_window = tk.Tk()
    gui_instance = ControlGUI(app_window)
    app_window.mainloop()
