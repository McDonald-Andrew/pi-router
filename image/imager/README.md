# Pinned Raspberry Pi Imager inputs

These files are reviewed build inputs, not moving downloads:

- `official-os-entry.json` is the official Desktop entry matching the base
  image pinned by `../base-image.env`.
- `official-pi5-device.json` is the matching official Raspberry Pi 5 device
  entry.
- `os-list-schema.json` is the official Raspberry Pi Imager OS-list schema at
  commit `88d84f3797753c77496da48aafddbdf242c2a948` with SHA-256
  `672390bc25c98d191ac43875b2ae85d5b6403ca7afce3a854e09d976277686b9`.

The OS and device entries were captured from
`https://downloads.raspberrypi.com/os_list_imagingutility_v4.json` on
2026-11-06. The schema source is:

`https://github.com/raspberrypi/rpi-imager/blob/88d84f3797753c77496da48aafddbdf242c2a948/doc/json-schema/os-list-schema.json`

Updating any pinned input requires updating its tests and reviewing the exact
difference from the previous official entry.
