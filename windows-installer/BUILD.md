# PROTON ROUTER INSTALLER

## Purpose

One-window Windows orchestrator for the certified Raspberry Pi 5 Proton router setup.
The Linux `src/setup` phases remain unchanged and available for manual installation.

## Debugging / Development and Building Locally

To view UI run:

```powershell
cd windows-installer

dotnet watch
```

### Build Locally

Place the `proton-router-vX.Y.Z.tar.gz` archive in the repository root before running the build script.

Install the .NET 8 SDK, then run:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows-installer\build.ps1
```

The publish folder will contain files including:

    proton-router-installer.exe
    proton-router-installer.sha256.txt
    THIRD-PARTY-NOTICES.txt

GitHub Actions `Proton Router Release` workflow packages `src/`, creates the archive, builds the matching win-x64 installer, and uploads all release files.

## Release Folder Used by the Installer

Place these files together:

    proton-router-installer.exe
    proton-router-v*.*.*.tar.gz
    proton-router-v*.*.*.sha256.txt

Then run the installer within that folder.

## What the GUI Automates

- verifies the local filename + SHA-256 + sidecar
- connects to the fresh Pi using SSH.NET password authentication
- pins the Pi SSH host-key fingerprint in memory and requires it on later reconnects
- uploads by SFTP and verifies the Pi-side SHA-256
- extracts and checks both fixed Ethernet carrier roles
- runs the first pre-reboot pass over Wi-Fi
- software-cycles the selected Windows Ethernet adapter and renews DHCP
- verifies the provisioning lease is 192.168.50.x with blank gateway and DNS
- reconnects to 192.168.50.1 and finishes pre-reboot
- reboots the Pi and waits for LAN SSH to return
- runs interactive post-reboot, answering sudo and Proton prompts from the GUI fields
- verifies the managed Proton tunnel/proton0 checks
- renews the promoted router lease and verifies gateway/DNS are 192.168.50.1
- performs the final unattended reboot
- waits for the Pi to return and runs final-verify
- requires FINAL VERIFICATION PASSED

The installer never changes Windows interface metrics, static IP settings, or routes.
Passwords are not written to the installer log.
