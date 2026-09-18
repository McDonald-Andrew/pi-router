using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using Renci.SshNet;
using Renci.SshNet.Common;

namespace ProtonRouterInstaller;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        ApplicationConfiguration.Initialize();
        Application.Run(new InstallerForm());
    }
}

internal enum PromptMode
{
    None,
    SudoOnly,
    PostReboot
}

internal sealed record RemoteResult(int ExitCode, string Output, bool Disconnected = false);
internal sealed record ProcessResult(int ExitCode, string Output);

internal sealed class InstallerForm : Form
{
    private const string CertifiedArchiveName = "proton-router-v0.0.0.tar.gz";
    private const string CertifiedSidecarName = "proton-router-v0.0.0.sha256.txt";
    private const string CertifiedArchiveSha256 = "A00B8301CBEC80FD24E79FF23298E25B55DED0A8F725A8FDB4D28B622064B7FB";
    private const string LanHost = "192.168.50.1";
    private const string LanPrefix = "192.168.50.";
    private const string RemoteExportDir = "~/proton-router-v0.0.0";

    private readonly TextBox txtArchive = new();
    private readonly TextBox txtInitialHost = new();
    private readonly TextBox txtPiUser = new();
    private readonly TextBox txtPiPassword = new();
    private readonly TextBox txtProtonUser = new();
    private readonly TextBox txtProtonPassword = new();
    private readonly TextBox txtAdapter = new();
    private readonly Button btnBrowse = new();
    private readonly Button btnStart = new();
    private readonly Button btnCancel = new();
    private readonly ProgressBar progress = new();
    private readonly Label lblStatus = new();
    private readonly RichTextBox logBox = new();

    private CancellationTokenSource? cancelSource;
    private StreamWriter? logWriter;
    private string? logPath;
    private string piPassword = "";
    private string protonPassword = "";
    private string protonUser = "";
    private string piUser = "admin";
    private string? pinnedHostFingerprint;

    public InstallerForm()
    {
        Text = "Proton Router Installer";
        Width = 400;
        Height = 680;
        MinimumSize = new Size(760, 620);
        StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Segoe UI", 9F);

        BuildUi();
        LoadDefaults();
    }

    private void BuildUi()
    {
        var root = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(12),
            ColumnCount = 3,
            RowCount = 12
        };
        root.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 150F));
        root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
        root.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 92F));

        var topology = new Label
        {
            Text = "ENSURE THE FOLLOWING ARE CONNECTED FIRST BEFORE RUNNING INSTALLER:\nLaptop Ethernet -> built-in Pi RJ45 (eth0/LAN)\nRouter LAN -> Waveshare RJ45 (eth1/WAN)\nPi is reachable over Wi-Fi",
            Dock = DockStyle.Fill,
            ForeColor = Color.FromArgb(45, 45, 45),
            TextAlign = ContentAlignment.MiddleLeft,
            AutoEllipsis = true
        };
        root.Controls.Add(topology, 0, 0);
        root.SetColumnSpan(topology, 3);

        AddRow(root, 1, "Certified archive", txtArchive, btnBrowse);
        AddRow(root, 2, "Initial Pi host", txtInitialHost, null);
        AddRow(root, 3, "Pi username", txtPiUser, null);
        AddRow(root, 4, "Pi password", txtPiPassword, null);
        AddRow(root, 5, "Proton username", txtProtonUser, null);
        AddRow(root, 6, "Proton password", txtProtonPassword, null);
        AddRow(root, 7, "Windows Ethernet", txtAdapter, null);

        txtPiPassword.UseSystemPasswordChar = true;
        txtProtonUser.UseSystemPasswordChar = true;
        txtProtonPassword.UseSystemPasswordChar = true;

        txtPiPassword.Dock = DockStyle.Left;
        txtPiPassword.Width = 70;

        txtProtonUser.Dock = DockStyle.Left;
        txtProtonUser.Width = 70;

        txtProtonPassword.Dock = DockStyle.Left;
        txtProtonPassword.Width = 70;


        btnBrowse.Text = "Browse...";
        btnBrowse.Dock = DockStyle.Fill;
        btnBrowse.Click += BrowseClicked;

        var buttons = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.LeftToRight,
            AutoSize = true
        };
        btnStart.Text = "Install Router";
        btnStart.AutoSize = true;
        btnStart.Click += StartClicked;
        btnCancel.Text = "Cancel";
        btnCancel.AutoSize = true;
        btnCancel.Enabled = false;
        btnCancel.Click += (_, _) => cancelSource?.Cancel();
        buttons.Controls.Add(btnStart);
        buttons.Controls.Add(btnCancel);
        root.Controls.Add(buttons, 1, 8);
        root.SetColumnSpan(buttons, 2);

        lblStatus.Text = "Ready";
        lblStatus.Dock = DockStyle.Fill;
        lblStatus.TextAlign = ContentAlignment.MiddleLeft;
        lblStatus.AutoEllipsis = true;
        root.Controls.Add(lblStatus, 0, 9);

        progress.Dock = DockStyle.Fill;
        progress.Minimum = 0;
        progress.Maximum = 100;
        progress.Style = ProgressBarStyle.Continuous;
        root.Controls.Add(progress, 1, 9);
        root.SetColumnSpan(progress, 2);

        logBox.Dock = DockStyle.Fill;
        logBox.ReadOnly = true;
        logBox.Font = new Font("Consolas", 8.5F);
        logBox.BackColor = Color.White;
        logBox.WordWrap = false;
        root.Controls.Add(logBox, 0, 10);
        root.SetColumnSpan(logBox, 3);

        var privacy = new Label
        {
            Text = "Pi and Proton passwords remain in memory only and are redacted from the installer log.",
            Dock = DockStyle.Fill,
            ForeColor = Color.DimGray,
            TextAlign = ContentAlignment.MiddleLeft
        };
        root.Controls.Add(privacy, 0, 11);
        root.SetColumnSpan(privacy, 3);

        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 48F));
        for (int i = 1; i <= 7; i++) root.RowStyles.Add(new RowStyle(SizeType.Absolute, 32F));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 40F));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 30F));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 28F));

        Controls.Add(root);
    }

    private static void AddRow(TableLayoutPanel root, int row, string labelText, Control control, Control? third)
    {
        var label = new Label
        {
            Text = labelText,
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleLeft
        };
        control.Dock = DockStyle.Fill;
        root.Controls.Add(label, 0, row);
        root.Controls.Add(control, 1, row);
        if (third is null)
            root.SetColumnSpan(control, 2);
        else
            root.Controls.Add(third, 2, row);
    }

    private void LoadDefaults()
    {
        string exeDir = AppContext.BaseDirectory;
        txtArchive.Text = Path.Combine(exeDir, CertifiedArchiveName);
        txtInitialHost.Text = "rpi5-0";
        txtPiUser.Text = "admin";
        txtAdapter.Text = "Ethernet";
    }

    private void BrowseClicked(object? sender, EventArgs e)
    {
        using var dialog = new OpenFileDialog
        {
            Filter = "Certified router release (*.tar.gz)|*.tar.gz|All files (*.*)|*.*",
            FileName = CertifiedArchiveName
        };
        if (dialog.ShowDialog(this) == DialogResult.OK)
            txtArchive.Text = dialog.FileName;
    }

    private async void StartClicked(object? sender, EventArgs e)
    {
        if (!ValidateInputs()) return;

        SetBusy(true);
        cancelSource = new CancellationTokenSource();
        piPassword = txtPiPassword.Text;
        protonPassword = txtProtonPassword.Text;
        protonUser = txtProtonUser.Text.Trim();
        piUser = txtPiUser.Text.Trim();
        pinnedHostFingerprint = null;

        try
        {
            PrepareLog();
            await InstallAsync(cancelSource.Token);
            SetProgress(100, "INSTALLATION PASSED");
            AppendLog("\r\n=== SUCCESS ===\r\nArchive install and unattended final verification passed.\r\n");
            MessageBox.Show(this,
                "Proton router installation and final verification passed.",
                "Proton Router Installer",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
        }
        catch (OperationCanceledException)
        {
            SetStatus("Cancelled");
            AppendLog("\r\nInstallation cancelled.\r\n");
        }
        catch (Exception ex)
        {
            SetStatus("FAILED");
            AppendLog("\r\n=== FAILED ===\r\n" + ex.Message + "\r\n");
            MessageBox.Show(this,
                ex.Message + "\r\n\r\nNo further install phase was started after this failed gate.",
                "Installation failed",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
        }
        finally
        {
            ClearSecrets();
            logWriter?.Dispose();
            logWriter = null;
            SetBusy(false);
        }
    }

    private bool ValidateInputs()
    {
        if (!File.Exists(txtArchive.Text))
        {
            MessageBox.Show(this, "Place or select the certified tar.gz archive.", "Missing archive",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return false;
        }

        if (string.IsNullOrWhiteSpace(txtInitialHost.Text) ||
            string.IsNullOrWhiteSpace(txtPiUser.Text) ||
            string.IsNullOrEmpty(txtPiPassword.Text) ||
            string.IsNullOrEmpty(txtProtonUser.Text) ||
            string.IsNullOrEmpty(txtProtonPassword.Text) ||
            string.IsNullOrWhiteSpace(txtAdapter.Text))
        {
            MessageBox.Show(this, "Complete all fields before starting.", "Missing information",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return false;
        }
        return true;
    }

    private void PrepareLog()
    {
        string baseDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "ProtonRouterInstaller");
        string logDir = Path.Combine(baseDir, "logs");
        Directory.CreateDirectory(logDir);
        logPath = Path.Combine(logDir, "install-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + ".log");
        logWriter = new StreamWriter(logPath, false, new UTF8Encoding(false)) { AutoFlush = true };
        AppendLog("Proton Router Installer\r\n");
        AppendLog("Started: " + DateTime.Now.ToString("O") + "\r\n");
        AppendLog("Log: " + logPath + "\r\n\r\n");
    }

    private async Task InstallAsync(CancellationToken token)
    {
        string archive = Path.GetFullPath(txtArchive.Text);
        string archiveName = Path.GetFileName(archive);
        string initialHost = txtInitialHost.Text.Trim();
        string adapter = txtAdapter.Text.Trim();

        SetProgress(4, "Verifying certified release");
        if (!string.Equals(archiveName, CertifiedArchiveName, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("This installer accepts only " + CertifiedArchiveName + ".");

        string localSha = await Task.Run(() => ComputeSha256(archive), token);
        if (!string.Equals(localSha, CertifiedArchiveSha256, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Archive SHA-256 does not match the certified clean-room build.");

        string? sidecarPath = FindSidecar(archive);
        if (sidecarPath is null)
            throw new InvalidOperationException("Missing " + CertifiedSidecarName + " beside the archive.");
        string? sidecarSha = ReadSidecarHash(sidecarPath);
        if (!string.Equals(sidecarSha, CertifiedArchiveSha256, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException(".sha256.txt does not contain the certified SHA-256.");
        AppendLog("archive-sha256=PASS " + localSha + "\r\n");

        SetProgress(8, "Checking Windows Ethernet adapter");
        ProcessResult adapterCheck = await RunPowerShellAsync(
            "$a=Get-NetAdapter -Name '" + PsQuote(adapter) + "' -ErrorAction Stop; " +
            "Write-Output ($a.Name + '|' + $a.Status)", token, false);
        RequireSuccess(adapterCheck, "Windows Ethernet adapter was not found.");
        AppendLog("windows-adapter=PASS " + adapterCheck.Output.Trim() + "\r\n");

        SetProgress(12, "Connecting to fresh Pi over Wi-Fi");
        await WaitForSshAsync(initialHost, TimeSpan.FromSeconds(60), token, false);

        SetProgress(16, "Uploading certified release");
        await UploadArchiveAsync(initialHost, archive, archiveName, token);

        SetProgress(19, "Verifying uploaded archive");
        RemoteResult remoteHash = await RunRemoteAsync(initialHost,
            "sha256sum ~/" + ShQuote(archiveName), PromptMode.None,
            TimeSpan.FromMinutes(2), token);
        RequireRemoteSuccess(remoteHash, "Could not verify the uploaded archive on the Pi.");
        if (!remoteHash.Output.Contains(localSha, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Uploaded Pi archive SHA-256 does not match the Windows source archive.");
        AppendLog("remote-archive-sha256=PASS\r\n");

        SetProgress(22, "Extracting archive on Pi");
        RemoteResult extract = await RunRemoteAsync(initialHost,
            "cd ~ && rm -rf ~/proton-router-v0.0.0 && tar -xzf ~/" + ShQuote(archiveName) +
            " && chmod +x ~/proton-router-v0.0.0/setup" +
            " && cd ~/proton-router-v0.0.0 && ./setup --help >/dev/null",
            PromptMode.None, TimeSpan.FromMinutes(3), token);
        RequireRemoteSuccess(extract, "Failed to extract or validate archive on the Pi.");

        SetProgress(25, "Checking physical Ethernet links");
        RemoteResult carrier = await RunRemoteAsync(initialHost,
            "printf 'eth0='; cat /sys/class/net/eth0/carrier; printf 'eth1='; cat /sys/class/net/eth1/carrier",
            PromptMode.None, TimeSpan.FromMinutes(1), token);
        RequireRemoteSuccess(carrier, "Could not read Pi Ethernet carrier state.");
        string c = StripAnsi(carrier.Output).Replace("\r", "");
        if (!Regex.IsMatch(c, @"eth0=\s*1") || !Regex.IsMatch(c, @"eth1=\s*1"))
            throw new InvalidOperationException(
                "Both Ethernet links must be up: Dell -> built-in Pi RJ45, old-router LAN -> Waveshare RJ45.");
        AppendLog("ethernet-carrier=PASS\r\n");

        string preCommand = "cd " + RemoteExportDir +
                            " && ./setup --live --allow-live-install --phase pre-reboot" +
                            " --allow-network-cutover --allow-wifi-disable --non-interactive";

        string preResumeCommand =
            "sudo rm -f /var/lib/proton-router-setup/reboot-required.boot-id" +
            " && cd " + RemoteExportDir +
            " && ./setup --live --allow-live-install --phase pre-reboot-resume" +
            " --allow-network-cutover --allow-wifi-disable --non-interactive";

        SetProgress(30, "Running pre-reboot stage over Wi-Fi");
        RemoteResult firstPre = await RunRemoteAsync(initialHost, preCommand,
            PromptMode.SudoOnly, TimeSpan.FromMinutes(45), token, true);

        bool expectedHandoff = firstPre.Output.Contains("provisioning DHCP enabled", StringComparison.Ordinal) &&
                               firstPre.Output.Contains("refusing to disable Wi-Fi", StringComparison.Ordinal);
        bool preAlreadyComplete = firstPre.Output.Contains("pre-reboot stage complete", StringComparison.Ordinal) &&
                                  firstPre.Output.Contains("REBOOT REQUIRED before post-reboot", StringComparison.Ordinal);
        if (!expectedHandoff && !preAlreadyComplete)
            throw new InvalidOperationException("Initial pre-reboot stage stopped before the expected LAN handoff gate.");
        if (expectedHandoff) AppendLog("wifi-to-lan-handoff-gate=PASS\r\n");

        if (!preAlreadyComplete)
        {
            SetProgress(43, "Resetting Windows Ethernet for LAN handoff");
            await ResetEthernetAsync(adapter, token);
            await WaitForProvisioningLeaseAsync(adapter, token);

            SetProgress(47, "Connecting to Pi LAN");
            bool lanReady = await TryWaitForSshAsync(LanHost, TimeSpan.FromSeconds(25), token);
            if (!lanReady)
            {
                AppendLog("LAN SSH not ready; performing one additional software Ethernet link cycle.\r\n");
                await ResetEthernetAsync(adapter, token);
                await WaitForProvisioningLeaseAsync(adapter, token);
                lanReady = await TryWaitForSshAsync(LanHost, TimeSpan.FromSeconds(60), token);
            }
            if (!lanReady)
            {
                DialogResult retry = MessageBox.Show(this,
                    "Windows has the 192.168.50.x provisioning lease, but LAN SSH is still unavailable.\r\n\r\n" +
                    "Physically unplug/replug the Dell Ethernet cable once, then click Retry.",
                    "LAN link needs a physical reset",
                    MessageBoxButtons.RetryCancel,
                    MessageBoxIcon.Warning);
                if (retry != DialogResult.Retry) throw new OperationCanceledException(token);
                await WaitForProvisioningLeaseAsync(adapter, token);
                await WaitForSshAsync(LanHost, TimeSpan.FromSeconds(60), token, false);
            }

            SetProgress(52, "Completing pre-reboot stage over LAN");
            RemoteResult secondPre = await RunRemoteAsync(LanHost, preResumeCommand, PromptMode.SudoOnly, TimeSpan.FromMinutes(30), token, true, true);

            bool preRebootCompleted = secondPre.Output.Contains(
                    "pre-reboot stage complete",
                    StringComparison.Ordinal) &&
                secondPre.Output.Contains(
                    "REBOOT REQUIRED before post-reboot",
                    StringComparison.Ordinal);

            if (!preRebootCompleted)
            {
                if (!secondPre.Disconnected)
                {
                    throw new InvalidOperationException("LAN pre-reboot stage did not reach its reboot checkpoint.");
                }

                AppendLog("pre-reboot-ssh-disconnect=VERIFYING-CHECKPOINT\r\n");

                SetProgress(55, "SSH disconnected; verifying Pi checkpoint");

                await WaitForSshAsync(LanHost, TimeSpan.FromMinutes(2), token, false);

                RemoteResult checkpoint = await RunRemoteAsync(
                    LanHost,
                    "test \"$(sudo cat " +
                    "/var/lib/proton-router-setup/reboot-required.boot-id " +
                    "2>/dev/null)\" = " +
                    "\"$(cat /proc/sys/kernel/random/boot_id)\" " +
                    "&& printf 'PRE_REBOOT_CHECKPOINT_OK\\n'",
                    PromptMode.SudoOnly, TimeSpan.FromMinutes(1), token, true
                );

                if (checkpoint.ExitCode != 0 || !checkpoint.Output.Contains("PRE_REBOOT_CHECKPOINT_OK", StringComparison.Ordinal))
                {
                    throw new InvalidOperationException("SSH disconnected and the Pi did not have a valid pre-reboot checkpoint.");
                }

                AppendLog("pre-reboot-checkpoint=PASS after SSH disconnect\r\n");
            }
        }

        SetProgress(58, "Rebooting Pi after pre-reboot stage");
        await RebootPiAsync(token);
        await RecoverAfterPiRebootAsync(adapter, token);

        SetProgress(68, "Signing in to Proton and enabling unattended services");
        string postCommand = "cd " + RemoteExportDir +
                             " && ./setup --live --allow-live-install --phase post-reboot";
        RemoteResult post;

        try
        {
            post = await RunRemoteAsync(LanHost, postCommand, PromptMode.PostReboot, TimeSpan.FromMinutes(20), token, true);
        }
        catch (Exception ex) when (ex is IOException || ex is SshConnectionException)
        {
            AppendLog("post-reboot-ssh-disconnect=RETRY\r\n");
            SetProgress(68, "Reconnecting after Proton sign-in");

            await WaitForSshAsync(LanHost, TimeSpan.FromMinutes(2), token, true);

            AppendLog("post-reboot-resume=RETRY\r\n");

            post = await RunRemoteAsync(LanHost, postCommand, PromptMode.PostReboot, TimeSpan.FromMinutes(20), token, true);
        }
        if (!post.Output.Contains("post-reboot stage complete", StringComparison.Ordinal) ||
            !post.Output.Contains("FINAL REBOOT REQUIRED for unattended verification", StringComparison.Ordinal))
            throw new InvalidOperationException("Post-reboot Proton stage did not reach the final reboot checkpoint.");
        if (!post.Output.Contains("Proton managed connection verified", StringComparison.Ordinal) ||
            !post.Output.Contains("proton0 WireGuard interface verified", StringComparison.Ordinal))
            throw new InvalidOperationException("Managed Proton tunnel verification did not pass.");
        AppendLog("managed-proton=PASS\r\n");

        SetProgress(78, "Refreshing Windows DHCP after router promotion");
        await ResetEthernetAsync(adapter, token);
        await WaitForRouterLeaseAsync(adapter, token);

        SetProgress(83, "Performing final unattended reboot");
        await RebootPiAsync(token);
        await RecoverAfterPiRebootAsync(adapter, token);

        SetProgress(90, "Waiting for unattended Proton connection");
        await WaitForUnattendedProtonAsync(token);

        SetProgress(93, "Running final verification");
        string verifyCommand = "cd " + RemoteExportDir +
                               " && ./setup --live --allow-live-install --phase final-verify --non-interactive";
        RemoteResult verify = await RunRemoteAsync(LanHost, verifyCommand,
            PromptMode.SudoOnly, TimeSpan.FromMinutes(10), token, true);
        RequireRemoteSuccess(verify, "Final verification command failed.");
        if (!verify.Output.Contains("FINAL VERIFICATION PASSED", StringComparison.Ordinal))
            throw new InvalidOperationException("Final verification did not report FINAL VERIFICATION PASSED.");
        AppendLog("final-verification=PASS\r\n");
    }

    private async Task UploadArchiveAsync(string host, string localPath, string remoteName, CancellationToken token)
    {
        await Task.Run(() =>
        {
            token.ThrowIfCancellationRequested();
            using var client = CreateSftpClient(host);
            client.Connect();
            using var fs = File.OpenRead(localPath);
            client.UploadFile(fs, remoteName, true);
            client.Disconnect();
        }, token);
        AppendLog("archive-upload=PASS\r\n");
    }

    private SshClient CreateSshClient(string host)
    {
        var auth = new PasswordAuthenticationMethod(piUser, piPassword);
        var info = new ConnectionInfo(host, 22, piUser, auth)
        {
            Timeout = TimeSpan.FromSeconds(8)
        };
        var client = new SshClient(info)
        {
            KeepAliveInterval = TimeSpan.FromSeconds(5)
        };
        client.HostKeyReceived += HostKeyReceived;
        return client;
    }

    private SftpClient CreateSftpClient(string host)
    {
        var auth = new PasswordAuthenticationMethod(piUser, piPassword);
        var info = new ConnectionInfo(host, 22, piUser, auth)
        {
            Timeout = TimeSpan.FromSeconds(8)
        };
        var client = new SftpClient(info);
        client.HostKeyReceived += HostKeyReceived;
        return client;
    }

    private void HostKeyReceived(object? sender, HostKeyEventArgs e)
    {
        string received = e.FingerPrintSHA256;
        if (pinnedHostFingerprint is null)
        {
            pinnedHostFingerprint = received;
            e.CanTrust = true;
            AppendLog("pi-host-key-pinned=SHA256:" + received + "\r\n");
            return;
        }

        e.CanTrust = string.Equals(pinnedHostFingerprint, received, StringComparison.Ordinal);
        if (!e.CanTrust)
            AppendLog("pi-host-key=FAIL unexpected fingerprint\r\n");
    }

    private async Task<RemoteResult> RunRemoteAsync(
        string host,
        string command,
        PromptMode promptMode,
        TimeSpan timeout,
        CancellationToken token,
        bool showOutput = false,
        bool expectDisconnect = false)
    {
        return await Task.Run(() =>
        {
            token.ThrowIfCancellationRequested();
            using var client = CreateSshClient(host);
            client.Connect();
            using ShellStream shell = client.CreateShellStream("dumb", 160, 48, 1280, 800, 65536);

            // Drain the login banner/prompt before issuing our command.
            Thread.Sleep(150);
            if (shell.DataAvailable)
                _ = shell.Read();

            string marker = "__PRI_DONE_" + Guid.NewGuid().ToString("N") + "__";
            string wrapped = command + "; __pri_rc=$?; printf '\\n" + marker + "%s\\n' \"$__pri_rc\"";
            shell.WriteLine(wrapped);

            var output = new StringBuilder();
            var scan = new StringBuilder();
            bool protonUserSent = false;
            bool protonPasswordSent = false;
            DateTime deadline = DateTime.UtcNow + timeout;

            while (DateTime.UtcNow < deadline)
            {
                token.ThrowIfCancellationRequested();

                if (shell.DataAvailable)
                {
                    string chunk = shell.Read();
                    output.Append(chunk);
                    scan.Append(chunk);
                    if (showOutput) AppendLog(Sanitize(chunk));

                    HandlePrompts(shell, scan, promptMode, ref protonUserSent, ref protonPasswordSent);

                    Match done = Regex.Match(output.ToString(), Regex.Escape(marker) + @"(?<rc>[0-9]+)");
                    if (done.Success)
                    {
                        int rc = int.Parse(done.Groups["rc"].Value);
                        return new RemoteResult(rc, StripMarker(output.ToString(), marker));
                    }
                }
                else
                {
                    if (!client.IsConnected)
                    {
                        if (expectDisconnect)
                            return new RemoteResult(0, output.ToString(), true);
                        throw new IOException("SSH session disconnected before command completion.");
                    }
                    Thread.Sleep(100);
                }
            }

            throw new TimeoutException("Remote command timed out after " + timeout + ".");
        }, token);
    }

    private void HandlePrompts(
        ShellStream shell,
        StringBuilder scan,
        PromptMode mode,
        ref bool protonUserSent,
        ref bool protonPasswordSent)
    {
        string s = scan.ToString();

        int sudo = s.IndexOf("[sudo] password for", StringComparison.OrdinalIgnoreCase);
        if (sudo >= 0)
        {
            int colon = s.IndexOf(':', sudo);
            if (colon >= 0)
            {
                shell.WriteLine(piPassword);
                scan.Remove(0, Math.Min(colon + 1, scan.Length));
                s = scan.ToString();
            }
        }

        if (mode == PromptMode.PostReboot && !protonUserSent)
        {
            const string userPrompt = "Proton account username or email:";
            int pos = s.IndexOf(userPrompt, StringComparison.OrdinalIgnoreCase);
            if (pos >= 0)
            {
                shell.WriteLine(protonUser);
                protonUserSent = true;
                scan.Remove(0, Math.Min(pos + userPrompt.Length, scan.Length));
                s = scan.ToString();
            }
        }

        if (mode == PromptMode.PostReboot && protonUserSent && !protonPasswordSent)
        {
            int pos = s.IndexOf("Password:", StringComparison.Ordinal);
            if (pos >= 0)
            {
                shell.WriteLine(protonPassword);
                protonPasswordSent = true;
                scan.Remove(0, Math.Min(pos + "Password:".Length, scan.Length));
            }
        }

        if (scan.Length > 4096)
            scan.Remove(0, scan.Length - 2048);
    }

    private async Task RebootPiAsync(CancellationToken token)
    {
        try
        {
            await RunRemoteAsync(LanHost, "sudo reboot", PromptMode.SudoOnly,
                TimeSpan.FromMinutes(1), token, true, true);
        }
        catch (SshConnectionException)
        {
            // A transport reset is a normal result once reboot begins.
        }
        AppendLog("pi-reboot-requested=PASS\r\n");
    }

    private async Task RecoverAfterPiRebootAsync(string adapter, CancellationToken token)
    {
        AppendLog("waiting-for-pi-reboot...\r\n");
        // The Ethernet carrier can return before the Pi DHCP service is ready.
        // Give the Pi a little time before cycling the Windows adapter.
        await Task.Delay(TimeSpan.FromSeconds(12), token);

        await ResetEthernetAsync(adapter, token);
        await WaitForLanAddressAsync(adapter, token);
        await WaitForSshAsync(LanHost, TimeSpan.FromMinutes(2), token, true);
    }

    private async Task WaitForUnattendedProtonAsync(
        CancellationToken token)
    {
        AppendLog("waiting-for-unattended-proton...\r\n");

        DateTime deadline = DateTime.UtcNow.AddMinutes(3);

        while (DateTime.UtcNow < deadline)
        {
            token.ThrowIfCancellationRequested();

            try
            {
                bool connected = await Task.Run(() =>
                {
                    using var client = CreateSshClient(LanHost);
                    client.Connect();

                    using SshCommand command = client.RunCommand("protonvpn status 2>/dev/null || true");

                    string output = (command.Result ?? "") + (command.Error ?? "");

                    return output.Contains("Status: Connected", StringComparison.OrdinalIgnoreCase);
                }, token);

                if (connected)
                {
                    AppendLog("unattended-proton=PASS\r\n");
                    return;
                }
            }
            catch (SshAuthenticationException)
            {
                throw new InvalidOperationException(
                    "Pi SSH password was rejected while waiting for Proton autoconnect.");
            }
            catch (SshConnectionException)
            {
                // Pi may still be finishing boot.
            }
            catch (SshOperationTimeoutException)
            {
                // SSH may be temporarily unreachable while networking/VPN routing settles.
                // Keep polling until the overall WaitForSsh timeout expires.
            }
            catch (System.Net.Sockets.SocketException)
            {
                // Network may still be settling after the reboot.
            }

            await Task.Delay(TimeSpan.FromSeconds(3), token);
        }

        throw new InvalidOperationException("Proton did not autoconnect within 3 minutes after the final reboot.");
    }

    private async Task ResetEthernetAsync(string adapter, CancellationToken token)
    {
        AppendLog("windows-ethernet-link-cycle=" + adapter + "\r\n");
        string a = PsQuote(adapter);
        string script =
            "Disable-NetAdapter -Name '" + a + "' -Confirm:$false -ErrorAction Stop; " +
            "Start-Sleep -Seconds 2; " +
            "Enable-NetAdapter -Name '" + a + "' -Confirm:$false -ErrorAction Stop; " +
            "Start-Sleep -Seconds 2; " +
            "ipconfig /renew \"" + adapter.Replace("\"", "") + "\" | Out-Null";
        ProcessResult result = await RunPowerShellAsync(script, token, false);
        RequireSuccess(result, "Windows Ethernet software link cycle failed.");
    }

    private async Task WaitForProvisioningLeaseAsync(string adapter, CancellationToken token)
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(50);
        while (DateTime.UtcNow < deadline)
        {
            token.ThrowIfCancellationRequested();
            ProcessResult state = await ReadWindowsLanStateAsync(adapter, token);
            if (state.ExitCode == 0)
            {
                LanState parsed = LanState.Parse(state.Output);
                if (parsed.Ip.StartsWith(LanPrefix, StringComparison.Ordinal) &&
                    string.IsNullOrWhiteSpace(parsed.Gateway) &&
                    string.IsNullOrWhiteSpace(parsed.Dns))
                {
                    AppendLog("provisioning-lease=PASS ip=" + parsed.Ip + " gateway=blank dns=blank\r\n");
                    return;
                }
            }
            await Task.Delay(2000, token);
        }
        throw new InvalidOperationException(
            "Windows Ethernet did not receive the expected 192.168.50.x provisioning lease with blank gateway/DNS.");
    }

    private async Task WaitForRouterLeaseAsync(string adapter, CancellationToken token)
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(50);
        while (DateTime.UtcNow < deadline)
        {
            token.ThrowIfCancellationRequested();
            ProcessResult state = await ReadWindowsLanStateAsync(adapter, token);
            if (state.ExitCode == 0)
            {
                LanState parsed = LanState.Parse(state.Output);
                if (parsed.Ip.StartsWith(LanPrefix, StringComparison.Ordinal) &&
                    parsed.Gateway == LanHost &&
                    parsed.Dns.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                        .Contains(LanHost, StringComparer.Ordinal))
                {
                    AppendLog("router-lease=PASS ip=" + parsed.Ip + " gateway=" + parsed.Gateway + " dns=" + parsed.Dns + "\r\n");
                    return;
                }
            }
            await Task.Delay(2000, token);
        }
        throw new InvalidOperationException(
            "Windows Ethernet did not receive the promoted router lease (gateway/DNS 192.168.50.1).");
    }

    private async Task WaitForLanAddressAsync(string adapter, CancellationToken token)
    {
        // Give the Pi up to 2.5 minutes to finish booting and bring its
        // DHCP service back online.
        DateTime deadline = DateTime.UtcNow.AddMinutes(2.5);

        // Force an immediate DHCP attempt on the first loop.
        DateTime nextRenew = DateTime.MinValue;

        string lastState = "";

        while (DateTime.UtcNow < deadline)
        {
            token.ThrowIfCancellationRequested();
            ProcessResult state = await ReadWindowsLanStateAsync(adapter, token);

            if (state.ExitCode == 0)
            {
                LanState parsed = LanState.Parse(state.Output);

                string observed = "IP=" + parsed.Ip + "|GW=" + parsed.Gateway + "|DNS=" + parsed.Dns;

                // Only log when the network state changes so the log does not get flooded every two seconds.
                if (!string.Equals(observed, lastState, StringComparison.Ordinal))
                {
                    AppendLog("windows-lan-state=" + observed + "\r\n");
                    lastState = observed;
                }

                // Success: Windows received a DHCP address from Pi.
                if (parsed.Ip.StartsWith(LanPrefix, StringComparison.Ordinal))
                {
                    AppendLog(
                        "windows-dhcp-reacquire=PASS ip=" + parsed.Ip + "\r\n"
                    );
                    return;
                }
            }

            // Do not assign a static address. Ask Windows DHCP to try again periodically while the Pi continues booting.
            if (DateTime.UtcNow >= nextRenew)
            {
                AppendLog("windows-dhcp-renew=RETRY\r\n");

                string safeAdapter = adapter.Replace("\"", "");

                await RunPowerShellAsync(
                    "ipconfig /renew \"" + safeAdapter + "\" | Out-Null; exit 0",
                    token,
                    false
                );

                nextRenew = DateTime.UtcNow.AddSeconds(12);
            }
            await Task.Delay(2000, token);
        }

        throw new InvalidOperationException(
            "Windows Ethernet stayed in DHCP mode but did not reacquire a 192.168.50.x lease after repeated renewals. " +
            "Last observed state: " + (string.IsNullOrWhiteSpace(lastState) ? "unavailable" : lastState) + "."
        );
    }

    private Task<ProcessResult> ReadWindowsLanStateAsync(
        string adapter,
        CancellationToken token)
    {
        string a = PsQuote(adapter);

        string script =
            "$c=Get-NetIPConfiguration -InterfaceAlias '" +
            a +
            "' -ErrorAction SilentlyContinue; " +

            "$ips=@($c.IPv4Address | ForEach-Object { $_.IPAddress }); " +

            "$ip=($ips | Where-Object { $_ -like '" +
            LanPrefix +
            "*' } | Select-Object -First 1); " +

            "if(-not $ip){$ip=($ips | Select-Object -First 1)}; " +

            "$gw=($c.IPv4DefaultGateway | " +
            "Select-Object -First 1 -ExpandProperty NextHop); " +

            "$dns=((Get-DnsClientServerAddress " +
            "-InterfaceAlias '" +
            a +
            "' -AddressFamily IPv4 " +
            "-ErrorAction SilentlyContinue).ServerAddresses -join ','); " +

            "Write-Output ('IP=' + $ip + '|GW=' + $gw + '|DNS=' + $dns)";

        return RunPowerShellAsync(script, token, false);
    }

    private async Task WaitForSshAsync(string host, TimeSpan timeout, CancellationToken token, bool afterReboot)
    {
        if (await TryWaitForSshAsync(host, timeout, token)) return;
        throw new InvalidOperationException(afterReboot
            ? "Pi did not return on LAN SSH after reboot."
            : "Could not connect to Pi SSH at " + host + ".");
    }

    private async Task<bool> TryWaitForSshAsync(string host, TimeSpan timeout, CancellationToken token)
    {
        DateTime deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            token.ThrowIfCancellationRequested();
            try
            {
                bool ready = await Task.Run(() =>
                {
                    using var client = CreateSshClient(host);
                    client.Connect();
                    using SshCommand cmd = client.RunCommand("printf PRI_READY");
                    return cmd.ExitStatus == 0 && cmd.Result.Contains("PRI_READY", StringComparison.Ordinal);
                }, token);
                if (ready)
                {
                    AppendLog("ssh-ready=PASS " + host + "\r\n");
                    return true;
                }
            }
            catch (SshAuthenticationException)
            {
                throw new InvalidOperationException("Pi SSH password was rejected.");
            }
            catch (SshConnectionException)
            {
                // Expected while a reboot is in progress or before LAN handoff is ready.
            }
            catch (SshOperationTimeoutException)
            {
                // SSH may accept connections slowly while boot/network/VPN settles.
            }
            catch (System.Net.Sockets.SocketException)
            {
                // Expected while host/port is not yet reachable.
            }

            await Task.Delay(2500, token);
        }
        return false;
    }

    private async Task<ProcessResult> RunPowerShellAsync(string script, CancellationToken token, bool showOutput)
    {
        var psi = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };
        psi.ArgumentList.Add("-NoProfile");
        psi.ArgumentList.Add("-NonInteractive");
        psi.ArgumentList.Add("-ExecutionPolicy");
        psi.ArgumentList.Add("Bypass");
        psi.ArgumentList.Add("-Command");
        psi.ArgumentList.Add(script);

        using var process = new Process { StartInfo = psi };
        if (!process.Start()) throw new InvalidOperationException("Could not start Windows PowerShell.");

        Task<string> stdout = process.StandardOutput.ReadToEndAsync(token);
        Task<string> stderr = process.StandardError.ReadToEndAsync(token);
        await process.WaitForExitAsync(token);
        string output = (await stdout) + (await stderr);
        if (showOutput && !string.IsNullOrEmpty(output)) AppendLog(Sanitize(output));
        return new ProcessResult(process.ExitCode, output);
    }

    private static void RequireSuccess(ProcessResult result, string message)
    {
        if (result.ExitCode != 0)
            throw new InvalidOperationException(message + " Exit code: " + result.ExitCode + ".");
    }

    private static void RequireRemoteSuccess(RemoteResult result, string message)
    {
        if (result.ExitCode != 0)
            throw new InvalidOperationException(message + " Exit code: " + result.ExitCode + ".");
    }

    private static string ComputeSha256(string file)
    {
        using var sha = SHA256.Create();
        using var stream = File.OpenRead(file);
        return Convert.ToHexString(sha.ComputeHash(stream)).ToLowerInvariant();
    }

    private static string? FindSidecar(string archive)
    {
        string dir = Path.GetDirectoryName(archive) ?? AppContext.BaseDirectory;
        string preferred = Path.Combine(dir, CertifiedSidecarName);
        if (File.Exists(preferred)) return preferred;

        string alternate = archive + ".sha256.txt";
        if (File.Exists(alternate)) return alternate;

        return null;
    }

    private static string? ReadSidecarHash(string path)
    {
        string text = File.ReadAllText(path);
        Match match = Regex.Match(text, @"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])");
        return match.Success ? match.Value.ToLowerInvariant() : null;
    }

    private string Sanitize(string text)
    {
        string safe = text ?? "";
        if (!string.IsNullOrEmpty(piPassword)) safe = safe.Replace(piPassword, "[REDACTED]", StringComparison.Ordinal);
        if (!string.IsNullOrEmpty(protonPassword)) safe = safe.Replace(protonPassword, "[REDACTED]", StringComparison.Ordinal);
        if (!string.IsNullOrEmpty(protonUser)) safe = safe.Replace(protonUser, "[REDACTED-PROTON-USER]", StringComparison.OrdinalIgnoreCase);
        return safe;
    }

    private static string StripMarker(string output, string marker)
    {
        return Regex.Replace(output, Regex.Escape(marker) + @"[0-9]+\s*", "", RegexOptions.CultureInvariant);
    }

    private static string StripAnsi(string value)
    {
        return Regex.Replace(value, @"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "");
    }

    private static string PsQuote(string value) => (value ?? "").Replace("'", "''");
    private static string ShQuote(string value) => "'" + (value ?? "").Replace("'", "'\\''") + "'";


    private void ClearSecrets()
    {
        piPassword = "";
        protonPassword = "";
        protonUser = "";
        txtPiPassword.Text = "";
        txtProtonPassword.Text = "";
    }

    private void SetBusy(bool busy)
    {
        btnStart.Enabled = !busy;
        btnCancel.Enabled = busy;
        btnBrowse.Enabled = !busy;
        txtArchive.Enabled = !busy;
        txtInitialHost.Enabled = !busy;
        txtPiUser.Enabled = !busy;
        txtPiPassword.Enabled = !busy;
        txtProtonUser.Enabled = !busy;
        txtProtonPassword.Enabled = !busy;
        txtAdapter.Enabled = !busy;
    }

    private void SetProgress(int value, string status)
    {
        if (InvokeRequired)
        {
            BeginInvoke(() => SetProgress(value, status));
            return;
        }
        progress.Value = Math.Clamp(value, progress.Minimum, progress.Maximum);
        lblStatus.Text = status;
        AppendLog("\r\n>>> " + status + "\r\n");
    }

    private void SetStatus(string status)
    {
        if (InvokeRequired)
        {
            BeginInvoke(() => SetStatus(status));
            return;
        }
        lblStatus.Text = status;
    }

    private void AppendLog(string text)
    {
        string safe = Sanitize(text);
        if (InvokeRequired)
        {
            BeginInvoke(() => AppendLog(safe));
            return;
        }
        logBox.AppendText(safe);
        logBox.SelectionStart = logBox.TextLength;
        logBox.ScrollToCaret();
        logWriter?.Write(safe);
    }

    private sealed record LanState(string Ip, string Gateway, string Dns)
    {
        public static LanState Parse(string output)
        {
            string line = output.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries)
                .LastOrDefault(x => x.Contains("IP=", StringComparison.Ordinal)) ?? "";
            var fields = line.Split('|').Select(x => x.Split(new[] { '=' }, 2)).
                Where(x => x.Length == 2).ToDictionary(x => x[0], x => x[1], StringComparer.OrdinalIgnoreCase);
            return new LanState(
                fields.TryGetValue("IP", out string? ip) ? ip.Trim() : "",
                fields.TryGetValue("GW", out string? gw) ? gw.Trim() : "",
                fields.TryGetValue("DNS", out string? dns) ? dns.Trim() : "");
        }
    }
}
