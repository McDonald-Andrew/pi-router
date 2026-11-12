# Pi Router Image Hardware Certification

> **No current artifact is eligible for release certification.** The
> `development` profile is a development artifact. A `release` build must
> preserve its exact bytes and final filename from build through testing and
> publication, and it must still pass every gate below before release.

This procedure supplies the hardware evidence required by P1, P3, P4, P5,
P6, P7, P8, P9, and P11 of the image installation plan. It does not certify an
image merely by existing. Every row must be executed against the exact artifact
that will be published.

## Artifact identity

Start with the canonical
[`certification/receipt.example`](certification/receipt.example) field order.
Create a PASS receipt only after every required test below has passed. Failed
or incomplete runs remain lab records and must not be converted into a PASS
receipt.

Copy the image hashes and sizes from the audited build manifest, independently
calculate `manifest_sha256`, then verify the compressed image hash before every
card write. If the image is rebuilt, restart certification with a new record.
Use a non-identifying tester label such as `example-lab`; do not publish machine
IDs, SSH private keys, hardware serial numbers, Proton credentials, or Proton
session material.

Validate the completed receipt against the exact release files:

```bash
python3 image/certification/validate_receipt.py \
    pi-router-vX.Y.Z-pi5-arm64.certification.txt \
    pi-router-vX.Y.Z-pi5-arm64.img.xz \
    pi-router-vX.Y.Z-pi5-arm64.manifest.txt
```

The validator fails closed on malformed fields, filename or provenance
mismatches, changed manifest/image bytes, incorrect compressed or extracted
hashes and sizes, a manifest not marked `release`, missing hardware/matrix
PASS assertions, or an artifact at or above the release size limit. It streams
the XZ image rather than trusting its manifest declarations.

## Required lab topology

- Two Raspberry Pi 5 units for the identity-uniqueness test.
- Two supported Waveshare PCIe Gigabit Ethernet adapters.
- Two microSD cards written from the same image artifact.
- Controllable upstream DHCP/Internet connection.
- Downstream access point or switch and at least two test clients.
- A normal client and a deliberately configured bypass client.
- A client on the upstream/WAN segment for exposure testing.
- Controllable power for interruption tests.

Record the Pi model reported by `/proc/device-tree/model` and prove the
Waveshare controller reports PCI vendor/device `10ec:8168`. Record only the
expected model and PCI ID in published evidence.

## Test matrix

For every row record start/end UTC time, artifact SHA, observed state/status,
expected result, actual result, and PASS/FAIL. Preserve sanitized logs for a
failure; never collect credential-bearing terminal input.

| ID | Scenario | Required evidence |
|---|---|---|
| H01 | Raspberry Pi Imager opens the release manifest, requires `admin`, and writes/verifies the image | Imager exposes the official Desktop customization choices; the selected password/SSH mode is applied; first local console boot identifies Raspberry Pi OS Desktop ARM64 and the expected release |
| H02 | Exact Pi 5 and Waveshare hardware | Pi model matches; PCI `10ec:8168` present; setup chooses the intended LAN and WAN roles |
| H03 | Happy path from final topology | Browser onboarding succeeds without SSH/Windows; both reboot boundaries occur; state reaches `ready` |
| H04 | LAN cable absent at boot | Remains fail-closed at LAN-carrier status; reconnecting resumes without reimage |
| H05 | WAN cable absent at boot | Remains fail-closed at WAN-carrier status; reconnecting resumes without reimage |
| H06 | Delayed WAN DHCP | Waits without opening forwarding; DHCP arrival resumes provisioning |
| H07 | WAN route but no Internet/DNS/HTTPS | Repository gate waits safely; restored connectivity resumes provisioning |
| H08 | Invalid Proton credentials followed by valid credentials | First request advances no state, persists no password, and writes no username outside intended Proton client storage; second request succeeds |
| H09 | Proton connection failure | Normal DHCP promotion/readiness does not occur; bootstrap firewall gate closes |
| H10 | Browser exposure boundaries | HTTPS onboarding is reachable from provisioning LAN and unreachable from WAN/upstream client |
| H11 | Self-signed certificate flow | Certificate is unique to the booted Pi; documented trusted-LAN warning matches tested browsers |
| H12 | First reboot boundary | `pre-reboot-complete` is written only after the setup checkpoint; normal gateway/DNS is still absent |
| H13 | Final reboot boundary | `post-reboot-complete` precedes reboot; final verification runs only on the next boot |
| H14 | Normal client routing | External address is the Proton exit; DNS and firewall policy remain enforced |
| H15 | Bypass client routing | Permitted application traffic follows raw WAN while DNS/domain/firewall policy remains enforced |
| H16 | Proton loss after readiness | Normal traffic fails closed; bypass behavior remains exactly as documented |
| H17 | Production web UI and updater | Local API is healthy and a staged GitHub update preserves configuration and returns to verified operation |
| H18 | Persistent credential residue | Post-onboarding filesystem and journal scans contain neither submitted username nor password outside intended Proton client session material |
| H19 | Two-card identity uniqueness | Machine IDs, SSH host public-key fingerprints, and NetworkManager UUID sets differ between unit A and unit B |
| H20 | Network listeners | Listener inventory matches the lifecycle allowlist below, reflects the SSH choice made in Imager, and exposes no service on WAN |

The expected non-loopback server allowlist is:

| Lifecycle | Allowed listeners |
|---|---|
| Before provisioning starts | No router listener; if SSH was selected in Imager, TCP 22 may listen only on the provisioning LAN |
| Provisioning DHCP active | `dnsmasq` DHCP UDP 67 and DNS TCP/UDP 53, LAN only; optional selected SSH remains LAN-only |
| Browser onboarding active | Provisioning `dnsmasq` listeners plus onboarding HTTPS TCP 8080, LAN only; optional selected SSH remains LAN-only |
| Proton verified/final/ready | Production `dnsmasq` DHCP/DNS and production HTTP TCP 8080, LAN only; onboarding HTTPS is absent; optional selected SSH remains LAN-only |

Record `ss --listening --numeric --tcp --udp --process` at each lifecycle point.
Loopback-only operating-system sockets may be recorded separately, but any new
non-loopback socket, wildcard listener not constrained by firewall/interface,
SSH listener that does not match the Imager selection, or WAN-reachable service
fails certification until explicitly reviewed and added to this table.

## Power-interruption matrix

Use fresh cards or known clean snapshots as required. Never fabricate or edit a
setup checkpoint to force a result. Cut power at each boundary, restore power,
and record whether the controller resumes, remains safely waiting, or creates a
documented terminal error.

File-write boundaries cannot be hit deterministically by physically cutting
power on the immutable release image. Cover these in Linux fault-injection
tests against the exact source commit and attach that CI result to the hardware
record:

| ID | Automated injection boundary | Required result |
|---|---|---|
| FI01 | Before and after attempt/state file fsync | Previous valid record remains authoritative; no partial record is accepted |
| FI02 | Before and after atomic rename and directory fsync | Restart accepts only a fully valid durable record |
| FI03 | Before and after ready receipt/state writes | No malformed or contradictory readiness combination is accepted |

| ID | Interruption boundary | Required result |
|---|---|---|
| PWR04 | During full pre-reboot before fail-closed indicator | Retry or terminal behavior follows the recorded proof; never forwards |
| PWR05 | After fail-closed application, before setup checkpoint | Resume uses only the exact setup-owned late-stage indicator and prerequisites |
| PWR06 | After pre checkpoint, before image-state advance | Restart reconciles the checkpoint and requests the first reboot |
| PWR07 | During Proton credential submission/sign-in | Credentials are not persisted; retry remains possible unless a documented terminal invariant failed |
| PWR08 | After post checkpoint, before image-state advance | Restart reconciles to `post-reboot-complete` and requests final reboot |
| PWR09 | During final verification before checkpoint consumption | Remains non-ready and reruns/reports failure safely |
| PWR10 | After final checkpoint consumption, before ready write | Durable final attempt permits the narrow documented recovery to `ready` |
| PWR12 | During reboot request | Existing checkpoint/state causes the reboot boundary to be requested again safely |

A checkpoint corrupted during setup's non-atomic overwrite must remain terminal
and require reimaging. The image controller must never repair, replace, or
forge setup-owned checkpoint data.

## Identity comparison

On each unit collect only comparison-safe values in the private lab record:

```bash
cat /etc/machine-id
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
sudo nmcli -g connection.uuid connection show
```

Prove that corresponding values differ, then publish only the PASS/FAIL result.
Do not publish raw values or unsalted hashes of stable machine identities. If a
comparison digest is required inside the private lab record, use a keyed
one-time digest and destroy the key after the comparison. Never publish the
private SSH host key or raw machine identity.

## Credential-residue review

Use unique canary credentials created specifically for certification. After
onboarding, search persistent files and the journal for both canaries. Exclude
only the Proton client's intended keyring/session storage from the username
finding; the password must not appear anywhere inspectable. Destroy the test
account credentials after certification.

Also inspect process arguments during sign-in. The unchanged Proton CLI may
transiently expose the username in its `signin` argument. The password must
never appear in process arguments, environment, status records, or logs.

## Certification decision

Certification passes only when:

1. every required row passes against one immutable artifact SHA;
2. every interruption leaves the router fail-closed and follows documented
   recovery;
3. both cards prove identity uniqueness;
4. persistent credential scans pass;
5. the exact image remains below the selected distribution size limit; and
6. the validated receipt is supplied to a protected release-publish step whose
   human reviewer confirms that the referenced private lab record exists.

Any rebuild invalidates the result and requires the relevant tests to run again.
The validator proves that a well-formed receipt is bound to exact artifact
bytes; it cannot independently prove that a person actually performed the
physical tests asserted by that receipt.
