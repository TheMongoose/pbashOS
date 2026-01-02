import storage
import board
import digitalio
import usb_cdc
import usb_hid

# 1. Setup Keyboard Scan (Manual Matrix for M5Cardputer)
COLS = [board.IO13, board.IO15, board.IO3, board.IO4, board.IO5, board.IO6, board.IO7]
ROWS = [board.IO8, board.IO9, board.IO11]

pressed = False

try:
    col_pins = []
    for pin in COLS:
        p = digitalio.DigitalInOut(pin)
        p.direction = digitalio.Direction.INPUT
        p.pull = digitalio.Pull.UP
        col_pins.append(p)

    row_pins = []
    for pin in ROWS:
        p = digitalio.DigitalInOut(pin)
        p.direction = digitalio.Direction.OUTPUT
        p.value = 1
        row_pins.append(p)

    for r in row_pins:
        r.value = 0
        for c in col_pins:
            if c.value == 0:
                pressed = True
        r.value = 1
        if pressed: break

    for p in col_pins + row_pins:
        p.deinit()

except Exception as e:
    print("Boot Key Error:", e)

# 2. STORAGE CONTROL
if pressed:
    print(">> Maintenance Mode: Drive Enabled")
    storage.enable_usb_drive()
    storage.remount("/", readonly=False)
else:
    print(">> Stealth Mode: Drive Disabled")
    storage.disable_usb_drive()