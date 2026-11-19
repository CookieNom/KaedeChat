"""Registration consent inside the native clients' existing verification window."""

import html
import secrets

from fastapi.responses import HTMLResponse

from app.auth.turnstile import REGISTER_ACTION

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Review server policies</title>
<style nonce="@NONCE@">
:root { color-scheme:dark; font:16px/1.5 system-ui,sans-serif; color:#f4eee5; background:#171715 }
* { box-sizing:border-box }
body { margin:0; padding:clamp(12px,4vw,28px) }
main { max-width:34rem; margin:auto; padding:clamp(16px,4vw,28px); background:#242420;
  border:1px solid #46443e; border-radius:16px }
h1 { font-size:1.5rem; line-height:1.25; margin:0 0 16px }
details { border-top:1px solid #55514a }
details:last-of-type { border-bottom:1px solid #55514a }
summary { padding:11px 4px; font-weight:600; cursor:pointer; color:#ffb497 }
.policy { max-height:45vh; overflow:auto; padding:0 8px; overflow-wrap:anywhere }
.policy h2 { font-size:1rem; margin:16px 0 4px }
.policy p,.policy ul { margin:6px 0 12px }
.policy ul { padding-left:22px }
label { display:flex; align-items:flex-start; gap:10px; padding:12px 0; margin-top:6px }
input { flex:none; width:20px; height:20px; margin:2px 0 0; accent-color:#ffa784 }
button { font:inherit; font-weight:650; border:0; border-radius:9px; padding:10px 16px;
  min-height:44px; background:#ffa784; color:#271b14; cursor:pointer }
button:disabled { background:#403f39; color:#aaa59b; cursor:default }
#agree { width:100% }
#retry { background:transparent; border:1px solid #a39b8c; color:#f4eee5; margin-bottom:10px }
#status { font-size:.94rem; margin:8px 0; color:#e3dcd0 }
#status:empty { display:none }
#challenge:not(:empty) { margin:6px 0 }
:focus-visible { outline:2px solid #ffd1b6; outline-offset:2px }
</style></head><body>
<main data-request-id="@REQUEST@" data-site-key="@KEY@" data-action="@ACTION@">
<h1>Review server policies</h1>
<details><summary>Terms of Service</summary>
<div id="terms" class="policy" role="region" aria-label="Terms of Service" tabindex="0"></div>
</details>
<details><summary>Privacy Policy</summary>
<div id="privacy" class="policy" role="region" aria-label="Privacy Policy" tabindex="0"></div>
</details>
<form>
<label><input type="checkbox" id="accepted" disabled>
<span>I agree to this server’s Terms of Service and Privacy Policy.</span></label>
<div id="challenge"></div>
<p id="status" role="status">Loading the server’s policies…</p>
<button id="retry" type="button" hidden>Try again</button>
<button id="agree" type="submit" disabled>Agree and continue</button>
</form></main>
<script nonce="@NONCE@">
const scriptNonce = document.currentScript.nonce;
const {requestId, siteKey, action} = document.querySelector('main').dataset;
const accepted = document.getElementById('accepted');
const agree = document.getElementById('agree');
const status = document.getElementById('status');
const retry = document.getElementById('retry');
let policiesReady = false;
let token = '';
let submitting = false;

function update() {
  accepted.disabled = !policiesReady || submitting;
  agree.disabled = accepted.disabled || !accepted.checked || (siteKey && !token) || !retry.hidden;
}

function showError(message) {
  status.textContent = message;
  retry.hidden = false;
  update();
}

async function loadPolicy(name) {
  const options = {mode:'same-origin', credentials:'omit', cache:'no-cache'};
  let response = await fetch('/' + name + '.html', options);
  // The development server serves the route itself instead of the static build's HTML file.
  if (response.status === 404) response = await fetch('/' + name, options);
  if (!response.ok) throw new Error();
  const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
  const article = doc.querySelector('article.legal-body');
  // Only published operator policies have a date. Never display the fallback notice as terms.
  const date = article?.querySelector('.legal-updated');
  if (!date || !article.querySelector('section')) throw new Error();
  const destination = document.getElementById(name);
  // Copy only the article's visible text into new elements. Its scripts and links stay inert.
  const blocks = article.querySelectorAll('.legal-updated, section h2, section p, section ul');
  for (const block of blocks) {
    const element = document.createElement(block.localName);
    if (block.localName === 'ul') {
      for (const item of block.querySelectorAll('li')) {
        const li = document.createElement('li');
        li.textContent = item.textContent;
        element.append(li);
      }
    } else {
      element.textContent = block.textContent;
    }
    destination.append(element);
  }
}

async function prepare() {
  try {
    await Promise.all([loadPolicy('terms'), loadPolicy('privacy')]);
  } catch {
    showError('The server’s policies could not be loaded. Try again to review them.');
    return;
  }
  policiesReady = true;
  status.textContent = siteKey ? 'Complete the security check to continue.' : '';
  update();
  if (!siteKey) return;
  const script = document.createElement('script');
  script.nonce = scriptNonce;
  script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  const failed = () => {
    token = '';
    showError('The security check could not be completed. Try again.');
  };
  script.onerror = failed;
  script.onload = () => {
    const challenge = document.getElementById('challenge');
    try {
      window.turnstile.render(challenge, {
        sitekey:siteKey, action, size:challenge.clientWidth < 300 ? 'compact' : 'flexible',
        callback(value) { token = value; status.textContent = ''; retry.hidden = true; update() },
        'expired-callback':failed, 'error-callback':failed
      });
    } catch { failed() }
  };
  document.head.append(script);
}

accepted.addEventListener('change', update);
retry.addEventListener('click', () => { retry.disabled = true; location.reload() });
document.querySelector('form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (agree.disabled || submitting) return;
  submitting = true;
  agree.textContent = 'Continuing…';
  update();
  try {
    const bridge = window.ipc || window.KaedeChallenge;
    if (!bridge?.postMessage) throw new Error();
    const payload = {accepted_terms:true};
    if (siteKey) payload.turnstile_token = token;
    const response = await fetch('/api/v1/auth/native-registration-consent', {
      method:'POST', mode:'same-origin', credentials:'omit',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
    });
    if (!response.ok) throw new Error();
    const {receipt} = await response.json();
    if (typeof receipt !== 'string' || !receipt || receipt.length > 2048) throw new Error();
    bridge.postMessage(JSON.stringify({kind:'complete', request_id:requestId, value:receipt}));
  } catch {
    submitting = false;
    agree.textContent = 'Agree and continue';
    showError('Your agreement could not be confirmed. Try again to continue.');
  }
});
prepare();
</script></body></html>"""


def native_registration_page(request_id: str, site_key: str | None) -> HTMLResponse:
    nonce = secrets.token_urlsafe(18)
    body = (
        _PAGE.replace("@NONCE@", nonce)
        .replace("@ACTION@", REGISTER_ACTION)
        .replace("@REQUEST@", html.escape(request_id, quote=True))
        .replace("@KEY@", html.escape(site_key or "", quote=True))
    )
    return HTMLResponse(
        body,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; "
                f"script-src 'nonce-{nonce}' https://challenges.cloudflare.com; "
                f"style-src 'nonce-{nonce}'; connect-src 'self' https://challenges.cloudflare.com; "
                "frame-src https://challenges.cloudflare.com; img-src data: "
                "https://challenges.cloudflare.com; frame-ancestors 'none'; "
                "form-action 'none'; base-uri 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
