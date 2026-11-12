#!/bin/sh
# Temporary image-owned guard: package installation during provisioning
# pre-reboot phase must not start services before setup applies router policy.
exit 101
