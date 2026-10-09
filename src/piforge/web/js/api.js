// PiForge GUI — REST helpers and a reconnecting websocket.

/** Error from the PiForge API with the HTTP status and the server's `detail` message. */
export class ApiError extends Error {
  /**
   * @param {number} status HTTP status
   * @param {string} detail server message
   */
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function parse(res) {
  let body = null;
  try { body = await res.json(); } catch { body = null; }
  if (!res.ok) {
    let detail = body && body.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg || JSON.stringify(d)).join('; ');
    throw new ApiError(res.status, detail || res.statusText);
  }
  return body;
}

/**
 * GET a JSON resource.
 * @param {string} url
 * @returns {Promise<any>}
 */
export async function getJSON(url) {
  return parse(await fetch(url, { headers: { Accept: 'application/json' } }));
}

/**
 * POST a JSON body and parse the JSON answer.
 * @param {string} url
 * @param {object} [body]
 * @param {AbortSignal} [signal]
 */
export async function postJSON(url, body = {}, signal) {
  return parse(await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body), signal,
  }));
}

/** Absolute ws:// URL for a server path. */
export function wsURL(path) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}${path}`;
}

/**
 * A JSON websocket that reconnects after unexpected closes.
 * Callbacks: onMessage(obj), onOpen(), onClose().
 */
export class Socket {
  /**
   * @param {string} path server path, e.g. "/ws/twin"
   * @param {{onMessage?:Function,onOpen?:Function,onClose?:Function}} handlers
   */
  constructor(path, handlers = {}) {
    this.path = path;
    this.handlers = handlers;
    this.ws = null;
    this.closed = false;
    this.retry = 500;
    this.connect();
  }

  /** True when the socket is open. */
  get open() { return this.ws && this.ws.readyState === WebSocket.OPEN; }

  connect() {
    if (this.closed) return;
    const ws = new WebSocket(wsURL(this.path));
    this.ws = ws;
    ws.addEventListener('open', () => { this.retry = 500; this.handlers.onOpen?.(); });
    ws.addEventListener('message', (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      this.handlers.onMessage?.(msg);
    });
    ws.addEventListener('close', () => {
      this.handlers.onClose?.();
      if (this.closed) return;
      setTimeout(() => this.connect(), this.retry);
      this.retry = Math.min(this.retry * 2, 5000);
    });
  }

  /**
   * Send a JSON message; returns false when not connected.
   * @param {object} obj
   */
  send(obj) {
    if (!this.open) return false;
    this.ws.send(JSON.stringify(obj));
    return true;
  }

  /** Close for good (no reconnect). */
  close() {
    this.closed = true;
    this.ws?.close();
  }
}
