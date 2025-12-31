# boot.py
import storage
import board
import digitalio
import sdcardio
import os
import busio

# 1. READ-ONLY TOGGLE
# Hold G0 button (IO0) on boot to give PC write access.
# Otherwise, Cardputer gets write access.
button = digitalio.DigitalInOut(board.IO0)
button.direction = digitalio.Direction.INPUT
button.pull = digitalio.Pull.UP

if button.value == False:
    # Button is PRESSED (Low)
    print("Boot: Safe Mode (PC Write Access)")
else:
    # Button is RELEASED (High)
    print("Boot: CardOS Mode (Device Write Access)")
    storage.remount("/", readonly=False)

# 2. SD CARD MOUNT
# M5Stack Cardputer SD Pins: SCK=40, MISO=39, MOSI=14, CS=12
try:
    spi = busio.SPI(clock=board.IO40, MOSI=board.IO14, MISO=board.IO39)
    sd = sdcardio.SDCard(spi, board.IO12)
    vfs = storage.VfsFat(sd)
    storage.mount(vfs, "/sd")
    print("SD Card mounted at /sd")
except Exception as e:
    print("No SD Card:", e)