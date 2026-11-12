(() => {
  "use strict";

  const CREDENTIAL_VERSION = 1;
  const MAX_USERNAME_BYTES = 254;
  const MAX_PASSWORD_BYTES = 1024;
  const CONTENT_TYPE = "application/vnd.pi-router.credentials";

  const csrfMeta = document.querySelector('meta[name="pi-router-csrf"]');
  const form = document.getElementById("onboarding-form");
  const usernameField = document.getElementById("proton-username");
  const passwordField = document.getElementById("proton-password");
  const submitButton = document.getElementById("submit-button");
  const result = document.getElementById("form-result");
  const productionHandoff = document.getElementById("production-handoff");
  const statusBadge = document.getElementById("status-badge");
  const statusSummary = document.getElementById("status-summary");
  const statusHardware = document.getElementById("status-hardware");
  const statusCables = document.getElementById("status-cables");
  const statusWan = document.getElementById("status-wan");
  const statusSetup = document.getElementById("status-setup");
  const statusError = document.getElementById("status-error");
  const encoder = new TextEncoder();
  let onboardingSubmitted = false;
  let runningPostObserved = false;

  function showProductionHandoff() {
    productionHandoff.hidden = false;
  }

  function hideProductionHandoff() {
    productionHandoff.hidden = true;
  }

  function containsForbidden(value) {
    return /[\u0000-\u001f\u007f]/u.test(value);
  }

  function buildCredentialFrame(username, password) {
    let usernameBytes = null;
    let passwordBytes = null;
    let frame = null;
    try {
      usernameBytes = encoder.encode(username);
      passwordBytes = encoder.encode(password);
      if (
        usernameBytes.byteLength < 1 ||
        usernameBytes.byteLength > MAX_USERNAME_BYTES ||
        passwordBytes.byteLength < 1 ||
        passwordBytes.byteLength > MAX_PASSWORD_BYTES
      ) {
        throw new Error("length");
      }

      frame = new Uint8Array(5 + usernameBytes.byteLength + passwordBytes.byteLength);
      const header = new DataView(frame.buffer);
      header.setUint8(0, CREDENTIAL_VERSION);
      header.setUint16(1, usernameBytes.byteLength, false);
      header.setUint16(3, passwordBytes.byteLength, false);
      frame.set(usernameBytes, 5);
      frame.set(passwordBytes, 5 + usernameBytes.byteLength);
      return frame;
    } finally {
      if (usernameBytes !== null) {
        usernameBytes.fill(0);
      }
      if (passwordBytes !== null) {
        passwordBytes.fill(0);
      }
    }
  }

  function presentStatus(documentValue) {
    if (onboardingSubmitted && documentValue.status === "running-post-reboot") {
      runningPostObserved = true;
    }
    if (
      onboardingSubmitted &&
      runningPostObserved &&
      documentValue.status === "waiting-onboarding"
    ) {
      result.textContent =
        "Sign-in did not complete. Verify the credentials and try again.";
      onboardingSubmitted = false;
      runningPostObserved = false;
      hideProductionHandoff();
      submitButton.disabled = false;
      form.hidden = false;
    }
    if (onboardingSubmitted && documentValue.status === "rebooting-after-post") {
      result.textContent =
        "Proton was verified. The router is rebooting, so this page will disconnect.";
      showProductionHandoff();
      form.hidden = true;
    }
    statusBadge.textContent = documentValue.ready ? "Ready" : documentValue.status;
    statusBadge.classList.toggle("ready", Boolean(documentValue.ready));
    statusSummary.textContent = documentValue.summary;
    statusHardware.textContent = documentValue.hardware;
    statusCables.textContent = documentValue.cables;
    statusWan.textContent = documentValue.wan;
    statusSetup.textContent = documentValue.setup;
    statusError.textContent = documentValue.error;
    if (documentValue.ready) {
      form.hidden = true;
    }
  }

  async function refreshStatus() {
    try {
      const response = await fetch("/api/status", {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer"
      });
      if (response.status === 401) {
        statusBadge.textContent = "Refreshing session";
        statusSummary.textContent = "Creating a new local browser session.";
        window.location.reload();
        return;
      }
      const documentValue = await response.json();
      if (typeof documentValue.status === "string") {
        presentStatus(documentValue);
      }
    } catch (_error) {
      if (onboardingSubmitted) {
        statusBadge.textContent = "Router restarting";
        statusSummary.textContent =
          "The onboarding service disconnected. This is expected while the router reboots.";
        result.textContent =
          "Wait for the reboot to finish, then use the production address below.";
        showProductionHandoff();
      } else {
        statusBadge.textContent = "Reconnecting";
        statusSummary.textContent =
          "The router may be restarting. This page will keep checking.";
      }
    } finally {
      window.setTimeout(refreshStatus, 3000);
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    result.textContent = "";

    let username = usernameField.value.trim();
    let password = passwordField.value;
    if (!username || !password || containsForbidden(username) || containsForbidden(password)) {
      result.textContent = "Enter a valid Proton username and password.";
      return;
    }

    let frame = null;
    try {
      frame = buildCredentialFrame(username, password);
    } catch (_error) {
      result.textContent = "The username or password is too long.";
      return;
    } finally {
      usernameField.value = "";
      passwordField.value = "";
    }
    username = "";
    password = "";

    submitButton.disabled = true;
    result.textContent = "Sending credentials to the router…";
    try {
      const response = await fetch("/api/onboard", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        referrerPolicy: "no-referrer",
        headers: {
          "Content-Type": CONTENT_TYPE,
          "X-Pi-Router-CSRF": csrfMeta.content
        },
        body: frame
      });
      const responseBody = await response.json();
      if (response.status === 202) {
        onboardingSubmitted = true;
        runningPostObserved = false;
        result.textContent =
          "Sign-in started. If verification succeeds, the router will reboot automatically and this page will disconnect.";
        showProductionHandoff();
      } else if (response.status === 429) {
        result.textContent = "Too many attempts. Wait before trying again.";
      } else if (response.status === 423) {
        result.textContent = "An onboarding attempt is already in progress.";
      } else if (responseBody.error === "not-ready") {
        result.textContent = "The router is not ready for sign-in yet.";
      } else {
        result.textContent = "Sign-in could not start. Check the status and try again.";
      }
    } catch (_error) {
      result.textContent = "The router may be restarting. Check the status before retrying.";
    } finally {
      if (frame !== null) {
        frame.fill(0);
      }
      if (!onboardingSubmitted) {
        submitButton.disabled = false;
      }
    }
  });

  refreshStatus();
})();
