# Pi Router Image Subsystem

This directory builds a Raspberry Pi Imager-compatible Pi Router image and
owns its resumable first-boot flow. It consumes the existing versioned
`src/setup` release; it is not another router installer.

The dependency boundary is deliberate:

```text
windows-installer/ --> src/setup
image/             --> versioned src/setup release

src/setup          -X-> image/
windows-installer/ -X-> image/
```

Removing `image/` must not change the Windows installer or the manual Linux
installation path. Image code calls setup's public phases instead of copying
its NetworkManager, firewall, DNS, Proton, or service logic.

No image is hardware-certified or published as a stable release yet. See
[`USER-GUIDE.md`](USER-GUIDE.md) for the intended flashing/onboarding flow and
[`HARDWARE-CERTIFICATION.md`](HARDWARE-CERTIFICATION.md) for the gates that an
exact release image must pass.

Every successful build includes a schema-validated `.rpi-imager-manifest`
derived from the pinned official Desktop entry.

## One-command local build

From a committed, clean checkout on Debian or Ubuntu Linux:

```bash
bash image/build-local \
    0.1.0 \
    /path/to/output \
    /path/to/linux-work
```

The default profile is `development`. To create the immutable bytes that will
enter hardware certification:

```bash
bash image/build-local \
    0.1.0 \
    /path/to/output \
    /path/to/linux-work \
    release
```

`WORK_DIR` must be on a native Linux filesystem with about 16 GiB free. It
does not need to be empty: the verified official Raspberry Pi OS download is
cached there for later builds. Do not use an NTFS-mounted Windows path.
The builder also requires the `python3-jsonschema` package. When Raspberry Pi
Imager will run on Windows, put `OUTPUT_DIR` under `/mnt/c/...`; the generated
manifest will contain the corresponding Windows file URI. Do not move or
rename the image after the manifest is generated.

The builder emits:

```text
pi-router-vX.Y.Z-pi5-arm64-development.img.xz
pi-router-vX.Y.Z-pi5-arm64-development.img.xz.sha256
pi-router-vX.Y.Z-pi5-arm64-development.manifest.txt
pi-router-vX.Y.Z-pi5-arm64-development.rpi-imager-manifest
```

The `release` profile omits `-development` from those names. Its manifest says
`release`, not `certified`. It may be published
as a prerelease for testing, but may be promoted to a stable release only after
the complete hardware matrix passes against that exact compressed SHA-256
without a rebuild.

## Automated release build

The manually dispatched **Pi Router Release** workflow in
`.github/workflows/release.yml` is the single GitHub Actions entrypoint. It:

1. creates an unpublished draft prerelease for the requested version;
2. builds and audits the `release` image with this same
   `image/build-local` command;
3. uploads the image, checksum, provenance manifest, and Imager manifest
   directly as GitHub Release assets rather than storing a duplicate large
   Actions artifact;
4. builds and uploads the existing router archive and Windows installer; and
5. publishes only after the exact nine-file prerelease asset set is present.

The generated Imager manifest points at the exact `.img.xz` asset on that
GitHub prerelease. The prerelease remains explicitly uncertified. Test those
exact bytes on the required hardware, add the completed certification receipt,
run the ten-file final release-set validator, and only then promote the same
release without rebuilding the image.

If any build or upload job fails, the workflow removes the incomplete draft
and its tag so the version can be retried cleanly.

The lower-level command accepts a prebuilt release archive and checksum:

```bash
bash image/build-image \
    development \
    0.1.0 \
    /path/to/pi-router-v0.1.0.tar.gz \
    /path/to/pi-router-v0.1.0.sha256.txt \
    /path/to/output \
    /path/to/linux-work
```

## Simple build model

The builder does not construct an operating system from packages. It:

1. downloads the exact official Raspberry Pi OS Desktop 64-bit image
   pinned by [`base-image.env`](base-image.env);
2. verifies its committed compressed and extracted sizes and SHA-256 values;
3. mounts its two partitions and modifies files offline;
4. installs the exact versioned Pi Router release and image-owned first-boot
   files;
5. preserves the official cloud-init user-customization path and clears
   cloneable identity, network, credential, and log state;
6. audits the mounted root filesystem without executing target code;
7. compresses the result and mounts the exported image read-only for a second
   audit; and
8. writes its checksum and provenance manifest.

The final build step derives a local `.rpi-imager-manifest` from the
matching official Raspberry Pi OS Desktop entry. The local manifest keeps the
official customization format and applicable capabilities, restricts the
custom image to Pi 5 64-bit, and replaces its image-specific identity fields.
It also requires the Imager user step so the card cannot be flashed as a
finished router without choosing the `admin` credentials.

Release automation sets `PI_ROUTER_IMAGER_IMAGE_URL` to the final HTTPS release
asset URL. Local builds leave it unset and retain the local file-URI behavior.

The host never executes an ARM target binary. The build uses no pi-gen, QEMU,
binfmt, or chroot. Package installation and all hardware-dependent work happen
natively on the Raspberry Pi during first boot.

The only setup call made during offline customization is setup's existing,
host-safe staging interface for the boot configuration:

```bash
bash /mounted/opt/pi-router/current/setup \
    --root /mounted \
    --phase boot \
    --pcie-state missing \
    --non-interactive
```

This gives the Waveshare PCIe NIC the setting it needs to be visible on the
first real Pi boot while leaving the setting's implementation in `src/setup`.

## Raspberry Pi Imager and first-boot sequence

Open the release's `.rpi-imager-manifest` in Raspberry Pi Imager 2.x and select
the Pi Router entry it provides. In OS customization, enter the exact username
`admin`, choose its password, and choose the desired SSH authentication mode.
The Pi Router browser does not collect or alter the Linux password.

On the Pi, the image-owned firstboot service waits for the official cloud-init
target before it starts router provisioning.

The image creates a unique machine identity on the Pi, detects the built-in
and Waveshare Ethernet hardware, and waits for both cables and WAN access. It
then invokes the unchanged versioned setup release:

```bash
/opt/pi-router/current/setup \
    --live \
    --allow-live-install \
    --phase config \
    --answers /etc/pi-router-image.conf \
    --non-interactive

/opt/pi-router/current/setup \
    --live \
    --allow-live-install \
    --phase pre-reboot \
    --answers /etc/pi-router.conf \
    --allow-network-cutover \
    --allow-wifi-disable \
    --non-interactive
```

`pre-reboot` remains the authority for files, packages, NetworkManager
profiles, firewall policy, DNS/DHCP, services, cable topology, and its reboot
checkpoint. A temporary image-owned `policy-rc.d` prevents package maintainer
scripts from starting services early. Firstboot removes it only after setup's
checkpoint and the live fail-closed firewall are proven.

After the first reboot, the LAN-only HTTPS onboarding page asks for Proton
credentials and drives the unchanged interactive phase through a pseudo-
terminal:

```bash
/opt/pi-router/current/setup \
    --live \
    --allow-live-install \
    --phase post-reboot
```

After Proton succeeds and the final reboot occurs, firstboot runs:

```bash
/opt/pi-router/current/setup \
    --live \
    --allow-live-install \
    --phase final-verify \
    --non-interactive
```

State reaches `ready` only after exit status zero, the exact success markers,
and setup's reboot-checkpoint rules all pass. The image controller never
creates, repairs, or forges a setup-owned checkpoint.

The ownership boundary is concrete: setup owns
`/var/lib/pi-router-setup/reboot-required.boot-id`, while the image controller
owns `/var/lib/pi-router-image/` and its `pre-reboot-complete` and
`post-reboot-complete` states. Final readiness also requires setup's exact
`FINAL VERIFICATION PASSED` marker.

## Safety and release boundary

The distributable image contains no Proton credentials or session, Linux user
password, reusable machine ID, SSH host key, real bypass-device record,
generated router config, or shared onboarding TLS key. Raspberry Pi Imager
writes the OS access settings selected by the user; the Proton browser request
is bounded to the active onboarding attempt and is not written to image-owned
status or logs.

The release workflow still builds the existing Windows installer independently
from the image builder, then places both output sets in one prerelease. Stable
image publication is gated by a machine-readable hardware receipt starting
from [`certification/receipt.example`](certification/receipt.example). Validate
the completed receipt against the exact image and provenance manifest with:

```bash
python3 image/certification/validate_receipt.py \
    pi-router-vX.Y.Z-pi5-arm64.certification.txt \
    pi-router-vX.Y.Z-pi5-arm64.img.xz \
    pi-router-vX.Y.Z-pi5-arm64.manifest.txt
```

Before promoting the prerelease, download its assets into one directory, add
the completed certification receipt, and validate the exact ten-file set:

```bash
python3 image/certification/validate_release_set.py \
    X.Y.Z \
    FULL_SOURCE_COMMIT \
    /path/to/release-assets
```

Rebuilding changes the artifact and invalidates its hardware certification.
