/* Operator inbox. Queues instructions for a human; never runs anything. */

function csrfToken() {
  const fromCookie = document.cookie
    .split(";")
    .map(part => part.trim())
    .find(part => part.startsWith("hawknetic_research_csrf="));
  if (fromCookie) return decodeURIComponent(fromCookie.split("=").slice(1).join("="));
  return sessionStorage.getItem("research_csrf_token") || "";
}

const queue = document.querySelector("#queue");
const formStatus = document.querySelector("#form-status");

function showQueueMessage(text) {
  queue.replaceChildren();
  const notice = document.createElement("article");
  notice.textContent = text;
  queue.append(notice);
}

function renderMessages(messages) {
  queue.replaceChildren();
  for (const message of messages) {
    const card = document.createElement("article");
    const heading = document.createElement("h2");
    heading.textContent = message.title;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${message.priority} / ${message.target} / ${message.status} / ${message.message_id}`;
    const body = document.createElement("p");
    body.textContent = message.body;
    card.append(heading, meta, body);
    queue.append(card);
  }
  if (!messages.length) showQueueMessage("No messages are queued.");
}

async function loadQueue() {
  // A queue that cannot be read must say so; rendering nothing looked exactly
  // like an empty queue.
  try {
    const response = await fetch("/internal/operator-messages.json", { headers: { Accept: "application/json" } });
    if (!response.ok) {
      showQueueMessage(response.status === 403
        ? "This queue needs an admin session. Sign in again to load it."
        : `The queue could not be loaded (${response.status}). It may still hold messages.`);
      return;
    }
    const payload = await response.json();
    renderMessages(payload.messages || []);
  } catch (error) {
    showQueueMessage(`The queue could not be reached: ${error.message}`);
  }
}

document.querySelector("#operator-form").addEventListener("submit", async event => {
  event.preventDefault();
  const formElement = event.currentTarget;
  formStatus.textContent = "Queueing for manual review…";
  const form = new FormData(formElement);
  // The action header is what makes this unforgeable from another site: a
  // browser cannot attach it cross-origin without a preflight, and the server
  // sends no CORS headers.
  const headers = { "Content-Type": "application/json", "X-Research-Action": "queue-operator-message" };
  const token = csrfToken();
  if (token) headers["X-CSRF-Token"] = token;
  let response;
  try {
    response = await fetch("/internal/operator-messages", {
      method: "POST",
      headers,
      body: JSON.stringify({
        title: form.get("title"),
        body: form.get("body"),
        priority: form.get("priority"),
        target: form.get("target"),
      }),
    });
  } catch (error) {
    formStatus.textContent = `The message was not queued: ${error.message}`;
    return;
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    formStatus.textContent = `Message was not queued: ${payload.error || "request_failed"}`;
    return;
  }
  formStatus.textContent = "Queued. No automatic action was taken.";
  formElement.reset();
  await loadQueue();
});

loadQueue();
