(() => {
  "use strict";

  const CREDENTIAL_VERSION = 1;
  const MAX_USERNAME_BYTES = 254;
  const MAX_PASSWORD_BYTES = 1024;
  const CONTENT_TYPE = "application/vnd.pi-router.credentials";
  const PRODUCTION_URL = "http://192.168.50.1:8080/";
  const PRODUCTION_REDIRECT_DELAY_MS = 300000;

  const csrfMeta = document.querySelector('meta[name="pi-router-csrf"]');
  const form = document.getElementById("onboarding-form");
  const usernameField = document.getElementById("proton-username");
  const passwordField = document.getElementById("proton-password");
  const submitButton = document.getElementById("submit-button");
  const result = document.getElementById("form-result");
  const statusSummary = document.getElementById("status-summary");
  const encoder = new TextEncoder();
  let onboardingSubmitted = false;
  let runningPostObserved = false;
  let redirectScheduled = false;

  function updateCredentialMask(field) {
    const container = field.closest(".concealed-credential");
    container.classList.toggle("has-value", field.value.length > 0);
  }

  for (const field of [usernameField, passwordField]) {
    field.addEventListener("input", () => updateCredentialMask(field));
    field.addEventListener("change", () => updateCredentialMask(field));
    updateCredentialMask(field);
  }

  function scheduleProductionRedirect() {
    if (redirectScheduled) {
      return;
    }
    redirectScheduled = true;
    form.hidden = true;
    submitButton.disabled = true;
    result.textContent =
      "Proton was verified. The router is restarting. This page will open the router interface automatically when startup has had time to finish.";
    window.setTimeout(() => {
      window.location.replace(PRODUCTION_URL);
    }, PRODUCTION_REDIRECT_DELAY_MS);
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
      submitButton.disabled = false;
      form.hidden = false;
    }
    if (documentValue.status === "rebooting-after-post") {
      scheduleProductionRedirect();
    }
    statusSummary.textContent = documentValue.summary;
    if (documentValue.status === "ready") {
      window.location.replace(PRODUCTION_URL);
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
        statusSummary.textContent = "Creating a new local browser session.";
        window.location.reload();
        return;
      }
      const documentValue = await response.json();
      if (typeof documentValue.status === "string") {
        presentStatus(documentValue);
      }
    } catch (_error) {
      if (redirectScheduled) {
        statusSummary.textContent =
          "The router is restarting. The local interface will open automatically.";
      } else if (onboardingSubmitted) {
        statusSummary.textContent =
          "The onboarding connection was interrupted. This page will keep checking.";
      } else {
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
      updateCredentialMask(usernameField);
      updateCredentialMask(passwordField);
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
