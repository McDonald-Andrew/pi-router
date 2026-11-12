from __future__ import annotations

from pathlib import Path
import unittest


IMAGE_DIR = Path(__file__).resolve().parents[1]


class BuildImageContractTests(unittest.TestCase):
    def read(self, relative_path: str) -> str:
        return (IMAGE_DIR / relative_path).read_text(encoding="utf-8")

    def test_builder_has_only_development_and_release_profiles(self) -> None:
        builder = self.read("build")

        self.assertIn("development|release", builder)
        self.assertIn("profile=${4:-development}", builder)
        for retired in ("proof", "preload", "provision"):
            with self.subTest(profile=retired):
                self.assertNotRegex(builder, rf"(^|[| ]){retired}([| )]|$)")

    def test_release_profile_uses_the_final_name_without_claiming_certification(
        self,
    ) -> None:
        source = self.read("build")

        self.assertIn('if [[ "$profile" == release ]]', source)
        self.assertIn('image_name="pi-router-v${version}-pi5-arm64"', source)
        self.assertIn("certification_status=release", source)
        self.assertNotIn("certification_status=certified", source)

    def test_development_name_cannot_be_mistaken_for_a_release(self) -> None:
        source = self.read("build")

        self.assertIn(
            'image_name="pi-router-v${version}-pi5-arm64-development"', source
        )

    def test_both_profiles_receive_the_same_complete_firstboot_stack(self) -> None:
        source = self.read("customize-rootfs")

        profile_case_end = source.index("esac")
        first_stack_install = source.index("pi-router-firstboot.service")
        self.assertLess(profile_case_end, first_stack_install)
        self.assertNotIn(
            '[[ "$PI_ROUTER_IMAGE_PROFILE" ==', source[first_stack_install:]
        )
        self.assertNotIn(
            'if [[ "$PI_ROUTER_IMAGE_PROFILE"', source[first_stack_install:]
        )

    def test_embedded_and_external_metadata_require_release_status(self) -> None:
        customizer = self.read("customize-rootfs")
        rootfs = self.read("tests/verify-rootfs")

        self.assertIn("certification_status=release", customizer)
        self.assertIn("certification_status=release", rootfs)
        self.assertNotIn("certification_status=certified", customizer)
        self.assertNotIn("certification_status=certified", rootfs)

    def test_builder_never_executes_target_arm_binaries(self) -> None:
        sources = "\n".join(
            self.read(path)
            for path in (
                "build",
                "customize-rootfs",
                "tests/verify-rootfs",
                "tests/verify-image",
            )
        )

        self.assertNotIn("arch-test", sources)
        self.assertNotIn("qemu-aarch64", sources)
        self.assertNotIn("binfmt_misc", sources)
        self.assertNotRegex(sources, r"(?m)^\s*(?:sudo\s+)?chroot\s+")

    def test_one_commit_supplies_router_and_image_sources(self) -> None:
        source = self.read("build")

        self.assertIn(
            '[[ "$(git -C "$repository_root" rev-parse HEAD)" == "$source_commit" ]]',
            source,
        )
        self.assertIn("--untracked-files=all", source)
        self.assertIn(
            'die "src/ and image/ must be committed and clean before building"',
            source,
        )
        self.assertIn('"$source_commit:src"', source)
        self.assertIn('"$source_commit:image"', source)
        self.assertNotIn('cp -a -- "$script_dir/."', source)

    def test_builder_verifies_both_forms_of_the_pinned_base_image(self) -> None:
        source = self.read("build")

        for value in (
            "BASE_IMAGE_FILENAME",
            "BASE_IMAGE_URL",
            "BASE_IMAGE_COMPRESSED_SIZE",
            "BASE_IMAGE_COMPRESSED_SHA256",
            "BASE_IMAGE_EXTRACTED_SIZE",
            "BASE_IMAGE_EXTRACTED_SHA256",
        ):
            with self.subTest(value=value):
                self.assertIn(value, source)
        self.assertIn("base-image-cache", source)
        self.assertIn("curl", source)
        self.assertIn("sha256sum", source)
        self.assertIn('BASE_IMAGE_FILENAME="${BASE_IMAGE_URL##*/}"', source)
        self.assertIn("invalid base image filename", source)
        self.assertNotRegex(source, r"(?i)(?:resolve|download).{0,20}latest")

    def test_offline_customizer_and_static_audits_are_the_only_image_steps(self) -> None:
        source = self.read("build")

        self.assertIn("customize-rootfs", source)
        self.assertIn("tests/verify-rootfs", source)
        self.assertIn("tests/verify-image", source)
        self.assertRegex(source, r"\blosetup\b")
        self.assertRegex(source, r"\bmount\b")
        self.assertRegex(source, r"\bumount\b")

        customizer = self.read("customize-rootfs")
        self.assertIn('bash "$release_path/setup"', customizer)
        self.assertIn("--phase boot", customizer)
        self.assertIn("--pcie-state missing", customizer)
        self.assertNotIn("--phase files", customizer)
        self.assertNotIn("--phase packages", customizer)
        self.assertNotIn("apt-get", customizer)

    def test_customizer_embeds_the_complete_firstboot_stack(self) -> None:
        source = self.read("customize-rootfs")

        for artifact in (
            "pi-router-firstboot.timer",
            "pi-router-firstboot.service",
            "pi-router-onboarding.service",
            "pi-router-onboarding-backend.service",
            "pi-router-policy-rc.d",
            "pi-router-sshd.conf",
            "pi-router-ssh-retry.conf",
            "pi-router-onboarding.sysusers",
            "pi_router_image_controller.py",
            "pi_router_onboarding_pseudo_terminal.py",
            "onboarding_server.py",
            "onboarding-index.html",
            "onboarding-app.js",
        ):
            with self.subTest(artifact=artifact):
                self.assertIn(artifact, source)
        self.assertIn('"$release_path/web/onboarding-index.html"', source)
        self.assertIn('"$release_path/web/onboarding-app.js"', source)
        self.assertIn('"$release_path/web/onboarding_server.py"', source)
        self.assertNotIn('"$firstboot_source/onboarding-index.html"', source)
        self.assertNotIn('"$firstboot_source/onboarding-app.js"', source)
        self.assertNotIn(
            '"$release_path/web/pi_router_onboarding_pseudo_terminal.py"', source
        )
        self.assertNotIn('"$firstboot_source/onboarding_server.py"', source)
        self.assertIn(
            'systemd-sysusers --root="$root" pi-router-onboarding.conf', source
        )
        self.assertIn("for unit in cups.service cups.socket cups.path", source)
        self.assertIn(
            'ln -sfnT /dev/null "$root/etc/systemd/system/$unit"', source
        )
        self.assertIn(
            '\\( -lname "$unit" -o -lname "*/$unit" \\) -delete', source
        )
        self.assertIn("systemctl --root=", source)
        self.assertIn("enable pi-router-firstboot.timer", source)

    def test_onboarding_reuses_the_production_stylesheet(self) -> None:
        customizer = self.read("customize-rootfs")
        rootfs_audit = self.read("tests/verify-rootfs")

        self.assertFalse((IMAGE_DIR / "firstboot/onboarding-styles.css").exists())
        self.assertIn('"$release_path/web/styles.css"', customizer)
        self.assertIn('"$release_path/web/styles.css"', rootfs_audit)
        self.assertIn('"$release_path/web/favicon.svg"', customizer)
        self.assertIn('"$release_path/web/favicon.svg"', rootfs_audit)
        self.assertNotIn("onboarding-styles.css", customizer)
        self.assertNotIn("onboarding-styles.css", rootfs_audit)

    def test_release_permissions_are_normalized_and_audited(
        self,
    ) -> None:
        customizer = self.read("customize-rootfs")
        rootfs_audit = self.read("tests/verify-rootfs")

        assignment = (
            'repository_metadata="$release_path/config/proton-repository.conf"'
        )
        self.assertIn(assignment, customizer)
        self.assertIn('chmod 0644 "$repository_metadata"', customizer)
        self.assertIn(assignment, rootfs_audit)
        self.assertIn(
            'assert_file "$repository_metadata" "0:0:644"', rootfs_audit
        )
        self.assertIn(
            'find "$release_path" -xdev \\( -type f -o -type d \\) '
            "-exec chmod go-w {} +",
            customizer,
        )
        self.assertIn(
            'fail "versioned release contains a non-root-owned entry:',
            rootfs_audit,
        )
        self.assertIn(
            'fail "versioned release contains a group/other-writable entry:',
            rootfs_audit,
        )

    def test_customizer_preserves_official_imager_user_customization(self) -> None:
        source = self.read("customize-rootfs")

        self.assertIn(
            "pi:x:1000:1000::/home/pi:/usr/sbin/nologin",
            source,
        )
        self.assertIn("pinned base UID 1000 placeholder is not locked", source)
        self.assertNotIn("--login admin", source)
        self.assertNotIn("--move-home", source)
        self.assertNotIn('groupmod --root "$root" --new-name admin pi', source)
        self.assertNotIn("mask userconfig.service", source)
        self.assertIn("base_os=raspberry-pi-os", source)

        unit = self.read("firstboot/pi-router-firstboot.service")
        self.assertIn("Wants=cloud-init.target", unit)
        self.assertIn("After=local-fs.target cloud-init.target", unit)

    def test_ssh_policy_does_not_override_imager_authentication_choice(self) -> None:
        source = self.read("firstboot/pi-router-sshd.conf")
        retry = self.read("firstboot/pi-router-ssh-retry.conf")

        self.assertIn("ListenAddress 192.168.50.1", source)
        self.assertIn("AllowUsers admin", source)
        self.assertNotIn("PasswordAuthentication", source)
        self.assertNotIn("KbdInteractiveAuthentication", source)
        self.assertIn("After=NetworkManager.service", retry)
        self.assertIn("StartLimitIntervalSec=0", retry)
        self.assertIn("Restart=on-failure", retry)
        self.assertIn("RestartPreventExitStatus=", retry)
        self.assertIn("RestartSec=5s", retry)

        customizer = self.read("customize-rootfs")
        rootfs_audit = self.read("tests/verify-rootfs")
        target = (
            "/etc/systemd/system/ssh.service.d/"
            "60-pi-router-address-retry.conf"
        )
        self.assertIn(target, customizer)
        self.assertIn(target, rootfs_audit)

    def test_system_installer_normalizes_parent_directory_permissions(self) -> None:
        setup = (IMAGE_DIR.parent / "src" / "setup").read_text(encoding="utf-8")
        start = setup.index("install_system_source()")
        end = setup.index("install_system_tree()", start)
        installer = setup[start:end]

        self.assertNotIn("mkdir -p", installer)
        self.assertIn("run_root install", installer)
        self.assertIn("-o root", installer)
        self.assertIn("-g root", installer)
        self.assertIn("-m 0755", installer)
        self.assertIn("verify_directory_mode_owner()", setup)
        self.assertIn(
            '"$(root_path /usr/local/share/pi-router-web)"',
            setup,
        )

    def test_github_release_limit_is_fail_closed_and_recorded(self) -> None:
        source = self.read("build")

        self.assertIn("github_release_asset_limit_bytes=2147483648", source)
        self.assertIn(
            "(( compressed_size < github_release_asset_limit_bytes ))", source
        )
        self.assertIn("github_release_asset_limit_bytes=%s", source)
        self.assertIn("compressed_size=%s", source)
        self.assertIn("image_size=%s", source)

    def test_artifact_is_locked_audited_and_published_atomically(self) -> None:
        source = self.read("build")

        self.assertIn("fingerprint_artifact()", source)
        self.assertIn('getattr(os, "O_NOFOLLOW", 0)', source)
        self.assertIn("flock --nonblock", source)
        self.assertIn('rm -f -- "$output_lock_path"', source)
        self.assertIn("final read-only image audit", source)
        self.assertRegex(source, r"\bmv\s+--\s+")

    def test_builder_releases_the_first_raw_copy_before_export_audit(self) -> None:
        source = self.read("build")

        release = source.index('rm -f -- "$raw_image"')
        final_audit = source.index("Running the final read-only image audit")
        self.assertLess(release, final_audit)

    def test_builder_emits_the_validated_local_imager_manifest(self) -> None:
        source = self.read("build")

        self.assertIn('imager_manifest="$output_dir/$image_name.rpi-imager-manifest"', source)
        self.assertIn('python3 "$image_source/generate-imager-manifest.py"', source)
        self.assertIn('--published-image-path "$artifact"', source)
        self.assertIn('--compressed-sha256 "$compressed_sha"', source)
        self.assertIn('--extracted-sha256 "$image_sha"', source)
        self.assertIn('mv -- "$imager_manifest_stage" "$imager_manifest"', source)

        self.assertIn("PI_ROUTER_IMAGER_IMAGE_URL", source)
        self.assertIn('imager_location_args=(--published-image-path "$artifact")', source)
        self.assertIn('imager_location_args=(--image-url "$PI_ROUTER_IMAGER_IMAGE_URL")', source)
        self.assertIn('"${imager_location_args[@]}"', source)

        generator = self.read("generate-imager-manifest.py")
        self.assertIn("jsonschema.Draft7Validator", generator)
        self.assertIn('"user_required"', generator)
        self.assertIn('["pi5-64bit"]', generator)
        self.assertIn("validate_approved_differences", generator)

    def test_builder_packages_only_committed_router_source(self) -> None:
        source = self.read("build")

        self.assertIn("VERSION OUTPUT_DIR WORK_DIR [PROFILE]", source)
        self.assertIn("src/ and image/ must be committed and clean", source)
        self.assertIn("--format=tar.gz", source)
        self.assertIn('"$source_commit:src"', source)
        self.assertIn('archive="$temporary_root/$release_name.tar.gz"', source)
        self.assertNotIn("ARCHIVE CHECKSUM", source)


if __name__ == "__main__":
    unittest.main()
