#!/bin/bash

nmcli connection down Hotspot 2>/dev/null
nmcli connection delete Hotspot 2>/dev/null

nmcli device wifi hotspot \
    ifname wlan0 \
    ssid LapTimer \
    password "laptimer123"

nmcli connection modify Hotspot \
    ipv4.method shared \
    ipv4.addresses 192.168.4.1/24

nmcli connection down Hotspot
nmcli connection up Hotspot