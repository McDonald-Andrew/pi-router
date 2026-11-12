# Raspberry Pi Imager Deployment Plan

## Status

The image subsystem is a simple offline customizer for one pinned official
Raspberry Pi OS Desktop image. It does not rebuild the operating system and
does not use QEMU, binfmt, chroot, or target ARM execution on the build host.

The OS-account plan is now locked to the official Raspberry Pi Imager flow:
the custom image preserves the official cloud-init mechanism, and the release
includes a local `.rpi-imager-manifest` derived from the matching official
Desktop entry. Raspberry Pi Imager creates `admin`, applies the password, and
applies the user's SSH choice. The Pi Router browser asks only for Proton
credentials.

Code, static checks, unit tests, and the completed development-image audit are
checked below. The verified development artifact is
`pi-router-v1.0.7-dev.1-pi5-arm64-development.img.xz` (1,480,322,372 bytes,
SHA-256 `cc4c5cf883a52a678a5e911125404782924664df3753955b9d4b49720aab5ce1`).
Items requiring Raspberry Pi Imager, physical Pi 5/Waveshare hardware, two
cards, release certification, or published release evidence remain
unchecked until that exact proof exists.

The current remaining path is intentionally short:

1. boot the development image on the required Pi 5/Waveshare hardware and
   correct only image-owned defects;
2. build one immutable `release` image and run the full hardware matrix;
3. publish only the exact certified bytes, hashes, manifests, and receipt.

## Objective

Produce a Raspberry Pi 5 disk image that Raspberry Pi Imager can write. After
the card is inserted and the final Ethernet topology is connected, the Pi
must first apply the OS account settings selected in Raspberry Pi Imager, then
run the existing setup flow, pause safely for a one-time browser-based Proton
sign-in, complete both required reboots, and reach
`FINAL VERIFICATION PASSED` without the Windows installer.

## Boundaries

- `src/setup` remains the single authority for router files, packages,
  NetworkManager profiles, firewall/DNS policy, Proton, services, and final
  verification.
- `image/` calls public setup phases; it does not copy their internal commands.
- `windows-installer/` remains unchanged and independently usable; the release
  workflow orchestrates its existing build alongside the separate image build.
- Removing `image/` must not alter either existing installation path.
- The distributable image contains no Proton credentials/session, private
  device identity, SSH host key, real bypass-device record, generated router
  configuration, or shared onboarding key.
- Hardware-dependent and network-dependent setup runs natively on the Pi, not
  while the image is built.

## Build and boot model

The builder verifies the exact official Raspberry Pi OS Desktop 64-bit
download pinned in `image/base-image.env`, mounts its partitions, and modifies
files offline. It installs the exact versioned `src/` release, first-boot
controller, browser onboarding service, safety guards, and build metadata. It
preserves the official locked user placeholder and cloud-init packages rather
than manufacturing an OS account. It sanitizes cloneable state, performs a
static mounted-root audit, compresses the image, and mounts the export
read-only for a second audit.

The release builder also derives a local `.rpi-imager-manifest` from the exact
official Desktop manifest entry. Only image identity fields, the deliberate
`user_required` policy, and the compatibility restriction to Pi 5 64-bit may
differ; the official `init_format` and applicable capabilities remain
authoritative.

The only setup call made by the builder is the existing host-safe staging call
needed before the Waveshare NIC can appear on the first Pi boot:

```bash
bash /mounted/opt/pi-router/current/setup \
    --root /mounted \
    --phase boot \
    --pcie-state missing \
    --non-interactive
```

On the Pi, firstboot calls the existing `config` and `pre-reboot` phases. The
unchanged `pre-reboot` phase owns file/package installation, hardware role
detection, network cutover, firewall/DNS policy, services, and the first reboot
checkpoint. After that reboot, the temporary LAN-only browser service drives
the unchanged interactive `post-reboot` phase. After the final reboot,
firstboot calls `final-verify` and creates readiness only on proven success.

## Progress points

### P0 - Freeze the integration contract

- [x] Document the exact public setup calls available to the image subsystem.
- [x] Use exit status, exact success markers, and setup-owned reboot
  checkpoints as the phase commit proofs.
- [x] Define image states `new`, `pre-reboot-complete`,
  `post-reboot-complete`, and `ready` under `/var/lib/pi-router-image/`.
- [x] Keep image, setup, and Windows-installer ownership independent.

### P1 - Customize a pinned official base image

- [x] Pin the exact official Raspberry Pi OS Desktop 64-bit filename,
  URL, compressed/extracted sizes, and SHA-256 values.
- [x] Fail closed unless both the compressed and extracted base image match the
  committed lock.
- [x] Verify the versioned Pi Router archive and bind it to the committed
  `src/` tree before extraction.
- [x] Install the release under `/opt/pi-router/releases/pi-router-vX.Y.Z/`
  with `/opt/pi-router/current` as the stable symlink.
- [x] Preserve the official locked UID/GID 1000 placeholder and cloud-init
  mechanism; do not create or rename `admin` offline.
- [x] Complete one end-to-end local development build and automatic export
  audit on Debian/Ubuntu Linux.
- [ ] Boot the resulting image on a Raspberry Pi 5.

Acceptance: a verified `.img.xz` boots Raspberry Pi OS Desktop on the required
Pi without having run any target ARM binary during image construction.

### P2 - Stage only first-boot prerequisites offline

- [x] Use setup's staging `boot` phase to add exactly one PCIe x1 setting and
  reject a Gen3 override without duplicating setup logic.
- [x] Install the complete image-owned provisioning/onboarding stack for both
  build profiles.
- [x] Install an early safety preset that leaves router and audited
  network-facing services disabled before provisioning.
- [x] Keep IPv4 forwarding off with a temporary image-owned sysctl guard.
- [x] Install a temporary `policy-rc.d` so package maintainer scripts cannot
  start services during firstboot setup.
- [x] Leave package download/installation, NetworkManager profiles, generated
  firewall/DNS files, and Proton work to native setup on the Pi.
- [x] Prove the staged files and service states against a completed exported
  image.

Acceptance: the image contains prerequisites and guards, not a second copy of
setup's installation logic or pre-generated machine-specific configuration.

### P3 - Prepare unique first-boot identity

- [x] Export `/etc/machine-id` in the documented uninitialized state and remove
  the D-Bus alias, random seed, cached NetworkManager state, and Bluetooth
  state.
- [x] Remove SSH host keys, stale legacy first-run data, authorized keys,
  histories, build logs, Proton/keyring state, and generated router files
  without removing the official cloud-init mechanism.
- [x] Generate NetworkManager profiles only through setup on the target Pi.
- [x] Keep the non-identifying hostname `pi-router`.
- [x] Scan the completed export and prove the sanitizer acceptance checks.
- [ ] Write the same image to two cards and prove machine IDs, SSH host keys,
  and NetworkManager UUID sets differ.

### P4 - Implement resumable first-boot orchestration

- [x] Install the timer-driven, locked firstboot controller.
- [x] Make state and attempt records atomic and durable with file and directory
  fsync boundaries.
- [x] Advance only from setup success/checkpoint proof; never forge or repair a
  setup-owned checkpoint.
- [x] Reconcile supported interrupted config, pre-reboot, post-reboot, and
  final-verification operations.
- [x] Bound command execution and retries while allowing cable/network waits to
  resume safely.
- [x] Fail closed on malformed state, ownership/mode violations, hardware
  mismatch, firewall failure, or final-verification failure.
- [ ] Inject interruption at every attempt/state fsync and rename boundary.
- [ ] Prove reboot and power-loss recovery on Pi hardware.

### P5 - Detect hardware and establish connectivity

- [x] Call unchanged setup `config` with `ROUTER_USER=admin` and empty
  `LAN_IF`/`WAN_IF` values so setup detects the physical roles.
- [x] Validate and read setup's root-owned generated configuration.
- [x] Wait independently for LAN carrier, WAN carrier, WAN DHCP route, DNS, and
  HTTPS access to the pinned Proton repository.
- [x] Create a temporary WAN bootstrap profile only when needed and remove it
  after setup installs its canonical profile.
- [ ] Prove all waits and the Pi 5/Waveshare `10ec:8168` role assignment on the
  required hardware.

### P6 - Run unchanged pre-reboot setup

- [x] Invoke the versioned `setup --phase pre-reboot` with the required live,
  answers, cutover, Wi-Fi-disable, and non-interactive flags.
- [x] Require its checkpoint and exact success markers before advancing.
- [x] Verify the canonical live firewall is fail-closed before enabling
  forwarding.
- [x] Remove the forwarding guard and package-service guard only after that
  proof, then request the first reboot.
- [ ] Prove the full package/network/firewall phase and first reboot on Pi
  hardware from a freshly written image.

### P7 - Provide one-time browser Proton onboarding

- [x] Start onboarding only after the first reboot boundary is proven.
- [x] Bind the temporary HTTPS frontend only to the provisioning LAN and use a
  unique runtime-only self-signed key generated on that Pi.
- [x] Show sanitized hardware, cable, WAN, setup, and error status.
- [x] Protect requests with origin/CSRF checks, bounded inputs, peer credential
  checks, and rate limiting.
- [x] Feed username/password only in response to the expected interactive pseudo-terminal
  prompts; exclude them from image-owned logs, status, and command arguments.
- [x] Require Proton and post-reboot success markers plus the new setup
  checkpoint before requesting the final reboot.
- [x] Keep failed authentication retryable without promoting normal gateway or
  DNS service.
- [ ] Prove the browser, certificate warning, credential-residue scan, LAN-only
  exposure, retries, and successful final-topology onboarding on hardware.

### P8 - Gate final readiness

- [x] Run final verification only after `post-reboot-complete` and a different
  boot ID prove the final reboot.
- [x] Wait a bounded period for the production Proton service.
- [x] Require `setup --phase final-verify` exit zero,
  `FINAL VERIFICATION PASSED`, and checkpoint consumption.
- [x] Create the ready receipt/state atomically only after those checks.
- [x] Stop the temporary onboarding services before normal operation.
- [ ] Prove final readiness and production updater behavior on hardware.

### P9 - Automated verification

- [x] Unit-test state transitions, command failure, timeout, interruption,
  reboot reconciliation, pseudo-terminal prompt splitting, redaction, request security, and fail-closed behavior.
- [x] Statically inspect the target dpkg database and systemd enablement
  without chroot or target execution.
- [x] Make the builder mount and audit the customized root before compression
  and the exported image read-only after compression.
- [x] Verify provenance metadata, service guards, the official locked user
  placeholder, cloud-init, PCIe config, firstboot assets, and absence of
  cloneable/private state.
- [x] Run the mounted-image audits against one completed real artifact.
- [ ] Complete the remaining durability fault-injection matrix.
- [ ] Complete the two-card identity test.

### P10 - Produce release artifacts

- [x] Provide one image command that packages committed `src/` and builds from
  that exact source commit.
- [x] Support only the clear `development` and `release` profiles.
- [x] Cache the verified official base while keeping each customization build
  isolated.
- [x] Emit `.img.xz`, `.sha256`, and provenance manifest outputs without
  changing the existing Windows installer build outputs.
- [x] Emit the matching `.rpi-imager-manifest` described in P12.
- [x] Build the immutable release in the release workflow and upload its four
  image outputs directly to a draft prerelease, without using large Actions
  artifact storage.
- [x] Complete a local build and confirm the compressed artifact is below the
  GitHub Releases 2 GiB per-asset limit.
- [ ] After P11, attach the exact certified image/checksum/manifest/receipt to a
  release through protected human review.

### P11 - Hardware-in-the-loop certification

- [ ] Flash the immutable release with Raspberry Pi Imager.
- [ ] Test the exact Pi 5 and Waveshare `10ec:8168` adapter.
- [ ] Complete every happy-path, cable, WAN, Proton, listener, routing, browser,
  updater, identity, and power-interruption row in
  `image/HARDWARE-CERTIFICATION.md`.
- [ ] Validate the PASS receipt against the exact image and manifest.
- [ ] Preserve the exact certified bytes without rebuilding before release.

### P12 - Mirror official Raspberry Pi Imager behavior

- [x] Lock the design to Raspberry Pi Imager 2.x and a local
  `.rpi-imager-manifest`; raw **Use Custom** is not the supported path because
  it defaults local images to no OS customization.
- [x] Keep OS username, password, and SSH selection out of the Pi Router
  browser; that page remains Proton-only.
- [x] Preserve the official cloud-init mechanism and avoid creating `admin`
  during offline image customization.
- [x] Capture the exact official Desktop manifest entry used by the pinned
  base image as reviewed build input.
- [x] Generate a local manifest that preserves official `init_format` and
  applicable capabilities, restricts compatibility to Pi 5 64-bit, replaces
  the custom image identity fields, and adds `user_required`.
- [x] Validate the generated manifest against the pinned official schema and fail
  unless an approved-difference test matches exactly.
- [x] Include the `.rpi-imager-manifest` in the build output and document opening
  it directly in Raspberry Pi Imager 2.x.
- [x] Require the user to enter the exact username `admin`, choose a password,
  and choose whether SSH is disabled, password-based, or key-based.
- [x] Start Pi Router firstboot only after the official cloud-init target.
- [x] Fail before router setup unless `admin` exists with UID/GID 1000, the
  expected home/shell, and a password set by the official customization flow.
- [x] Keep SSH bound to the provisioning LAN, preserve Imager's authentication
  choice, and start selected SSH only after the router firewall is proven.
- [ ] Verify those account and SSH gates on a flashed card.
- [x] Document final topology, browser URL, certificate warning, wait/retry
  behavior, diagnostics, recovery, and current development status.
- [x] Keep the Windows installer and manual Linux setup documented as
  independent alternatives.
- [ ] Publish the certified compressed/extracted hashes, sizes, both manifests,
  and hardware receipt.

## Definition of done

The image path is complete only when one immutable release passes
the full automatic audit and physical certification matrix, a user can open
the local manifest in Raspberry Pi Imager, configure `admin`, flash the card,
and finish Proton onboarding through the documented final topology. No private
or cloneable state may be shared between devices, and the exact certified
artifact set must be published. Until then, the Windows installer and manual
Linux setup remain the supported paths.
