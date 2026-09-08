/* Sign-in form. Kept out of the page so the CSP can refuse inline script. */

document.querySelector("#login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const status = document.querySelector("#login-status");
  const form = new FormData(event.currentTarget);
  status.textContent = "Signing in…";
  let response;
  try {
    response = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
    });
  } catch (error) {
    status.textContent = "Could not reach the sign-in service. Check your connection and try again.";
    return;
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    // The server will not say whether an account exists or is locked, so the
    // hint has to cover both without confirming either.
    status.textContent = payload.error === "invalid_credentials"
      ? "Sign-in failed. Check your username and password — repeated failures lock the account for a while."
      : "Sign-in is unavailable right now. If this continues, the account service may need attention.";
    return;
  }
  // The server also sets a readable CSRF cookie, which is what the app reads;
  // this copy only helps a tab opened before that cookie existed.
  sessionStorage.setItem("research_csrf_token", payload.csrf_token || "");
  window.location.assign("/");
});
