# Overlay touch snapshot extension

Operation `0x4c530100` reads physical touchscreen state for an overlay pointer. It
reuses `request_obj.vinput_info`; the shared mapping layout and old opcodes do not
change. Initialize touch using operation 4 first.

Before each request, clear `request_virtual_slots` to zero and set `slot` to the
previous physical slot, or -1 for a new pointer. A successful reply contains:

- `request_virtual_slots = 0x4c535431` (LST1 protocol signature).
- `POSITION_X`, `POSITION_Y`: panel axis maxima.
- `slot`: active physical slot, or -1 for pointer release.
- `x`, `y`: raw physical coordinates when a slot is active.

The input device event lock protects each snapshot. Virtual slots used for touch
output are excluded, so generated aim/trigger touches cannot click the overlay.
When the selected finger lifts, one release is reported before another physical
finger can be selected. Polling clients should run off the rendering thread and
release their UI pointer on errors. This is a state snapshot, not a lossless event
queue; contacts shorter than the polling interval can be missed.

Old drivers ignore unknown operations and leave the signature zero. Clients must
check it and report unsupported driver versions; a zero status alone is not proof
of support. Rebuild the main module using GitHub Actions `Build Android ARM64
drivers`. No kernel source or toolchain download is needed on the user's computer.
