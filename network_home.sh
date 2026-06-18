#!/bin/bash

nmcli connection down Hotspot 2>/dev/null

nmcli connection up netplan-wlan0-rx601m-be9aca-1
