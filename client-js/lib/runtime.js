/** Transport-agnostic request/response plumbing and SSE parsing.
 *
 * A line-by-line port of contree_client/runtime.py adapted to the
 * fetch platform: gzip decoding and connection pooling are handled by
 * fetch itself, so only the protocol logic lives here.
 */

export const CHUNK_SIZE = 65536;

export const PACKAGE_VERSION = "0.1.0";
export const UA_PRODUCT = `contree-client-js/${PACKAGE_VERSION}`;

const NODE_VERSION =
  typeof process !== "undefined" && process.versions?.node
    ? process.versions.node
    : null;
// browsers refuse to set the User-Agent header (a forbidden header
// name): the composed string is only ATTACHED as a header in Node,
// though userAgent() reports it everywhere
export const IS_NODE = NODE_VERSION !== null;
export const UA_RUNTIME =
  NODE_VERSION !== null ? `Node.js/${NODE_VERSION}` : "browser";
export const UA_PLATFORM =
  NODE_VERSION !== null
    ? `${process.platform}-${process.arch}`
    : (globalThis.navigator?.userAgent ?? "");

export const RETRY_DELAYS = Object.freeze([0.1, 0.2, 0.5, 1.0, 2.0, 5.0]);

// floor for reconnect loops that made no forward progress, so a server
// returning immediate empty streams does not spin the client
export const TIGHT_LOOP_FLOOR = 0.5;

/** An endless ladder of backoff delays: the ladder is walked once and
 * then the tail delay repeats forever. */
export function* retryDelays(delays = RETRY_DELAYS) {
  yield* delays;
  for (;;) {
    yield delays[delays.length - 1];
  }
}

/** Opt-in retries for transient failures of buffered requests.
 *
 * 425 (Too Early) and 429 (Too Many Requests) are a backend contract:
 * both mean the request was rejected before any processing, so
 * replaying them is always safe - even for a POST the caller hasn't
 * opted into unsafe retries for (see `call()`). */
export class RetryPolicy {
  constructor({
    statuses = [410, 425, 429],
    serverErrors = true,
    delays = RETRY_DELAYS,
    maxAttempts = 10,
    retryUnsafe = false,
  } = {}) {
    if (!delays.length) {
      throw new RangeError("RetryPolicy delays must not be empty");
    }
    for (const delay of delays) {
      if (!Number.isFinite(delay) || delay < 0) {
        throw new RangeError(
          `RetryPolicy delays must be finite and non-negative, got ${delay}`,
        );
      }
    }
    if (
      maxAttempts !== null &&
      (!Number.isInteger(maxAttempts) || maxAttempts < 1)
    ) {
      throw new RangeError("RetryPolicy maxAttempts must be an integer >= 1");
    }
    // defensive copies, frozen: a policy shared between clients must
    // not be mutable from the outside after validation
    this.statuses = Object.freeze([...statuses]);
    this.serverErrors = serverErrors;
    this.delays = Object.freeze([...delays]);
    this.maxAttempts = maxAttempts;
    this.retryUnsafe = retryUnsafe;
    Object.freeze(this);
  }

  retryableStatus(status) {
    if (this.statuses.includes(status)) {
      return true;
    }
    return this.serverErrors && status >= 500 && status < 600;
  }
}

/** Parse a Retry-After header: delta-seconds or an HTTP-date.
 * Negative values clamp to 0; anything unparsable reports null. */
export function parseRetryAfter(value) {
  if (value === null || value === undefined) {
    return null;
  }
  const seconds = Number(value);
  // Number("Infinity") parses: an infinite Retry-After would sleep
  // forever (or collapse into an immediate retry storm)
  if (Number.isFinite(seconds) && value.trim() !== "") {
    return Math.max(0, seconds);
  }
  const moment = Date.parse(value);
  if (Number.isNaN(moment)) {
    return null;
  }
  return Math.max(0, (moment - Date.now()) / 1000);
}

export function sleep(seconds) {
  return new Promise((resolve) => setTimeout(resolve, seconds * 1000));
}

export function monotonic() {
  return performance.now() / 1000;
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** True if *ref* looks like an image/operation UUID. */
export function isUuid(ref) {
  return UUID_RE.test(ref);
}

export function quotePath(value) {
  return encodeURIComponent(String(value));
}

export function encodeQuery(query) {
  return Object.entries(query)
    .flatMap(([key, value]) =>
      (Array.isArray(value) ? value : [value]).map(
        (item) =>
          `${encodeURIComponent(key)}=${encodeURIComponent(item).replaceAll("%2F", "/")}`,
      ),
    )
    .join("&");
}

export function formatTimeParam(value) {
  if (value instanceof Date) {
    return value.toISOString();
  }
  return String(value);
}

/** Parse an ISO 8601 timestamp, tolerating nanosecond precision. */
export function parseDatetime(value) {
  // Date.parse only accepts millisecond precision: trim the tail
  const trimmed = value.replace(/(\.\d{3})\d+/, "$1");
  const moment = new Date(trimmed);
  if (Number.isNaN(moment.getTime())) {
    throw new RangeError(`unparsable datetime: ${value}`);
  }
  return moment;
}

const textDecoder = new TextDecoder();
const textEncoder = new TextEncoder();

export function bytesToText(bytes) {
  return textDecoder.decode(bytes);
}

export function textToBytes(text) {
  return textEncoder.encode(text);
}

export function bytesToBase64(bytes) {
  if (typeof Buffer !== "undefined") {
    return Buffer.from(bytes).toString("base64");
  }
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

export function base64ToBytes(value) {
  if (typeof Buffer !== "undefined") {
    return new Uint8Array(Buffer.from(value, "base64"));
  }
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

/** The sha256 hexdigest of an upload payload.
 *
 * Accepts Uint8Array, string or Blob; a ReadableStream cannot be
 * re-read for the subsequent upload, so it reports null (the caller
 * skips deduplication). Blobs hash chunk by chunk in Node (10 GiB
 * must not materialize); browsers fall back to one arrayBuffer().
 */
export async function sha256(content) {
  if (typeof content === "string") {
    content = textToBytes(content);
  }
  if (content instanceof Uint8Array) {
    // the webcrypto global only appeared in Node 19: fall back to
    // node:crypto on Node 18 (browsers always have globalThis.crypto)
    const subtle = globalThis.crypto?.subtle;
    if (subtle === undefined) {
      const { createHash } = await import("node:crypto");
      return createHash("sha256").update(content).digest("hex");
    }
    const digest = await subtle.digest("SHA-256", content);
    return hex(new Uint8Array(digest));
  }
  if (typeof Blob !== "undefined" && content instanceof Blob) {
    if (NODE_VERSION !== null) {
      // node-only module, loaded lazily so browser bundlers never see it
      const { createHash } = await import("node:crypto");
      const digest = createHash("sha256");
      for await (const chunk of content.stream()) {
        digest.update(chunk);
      }
      return digest.digest("hex");
    }
    const digest = await globalThis.crypto.subtle.digest(
      "SHA-256",
      await content.arrayBuffer(),
    );
    return hex(new Uint8Array(digest));
  }
  return null;
}

function hex(bytes) {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
}

export function jsonBody(response) {
  return JSON.parse(bytesToText(response.body));
}

export function jsonObject(response) {
  const data = jsonBody(response);
  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    const type =
      data === null ? "null" : Array.isArray(data) ? "array" : typeof data;
    throw new TypeError(`expected a JSON object, got ${type}`);
  }
  return data;
}

export function jsonArray(response) {
  const data = jsonBody(response);
  if (!Array.isArray(data)) {
    const type = data === null ? "null" : typeof data;
    throw new TypeError(`expected a JSON array, got ${type}`);
  }
  return data;
}

/** Extract diagnostic fields from an unsuccessful response. */
export function responseErrorDetails(response) {
  let error = bytesToText(response.body);
  let traceback = null;
  let payload = null;
  try {
    payload = JSON.parse(error);
  } catch {
    payload = null;
  }
  if (
    payload !== null &&
    typeof payload === "object" &&
    !Array.isArray(payload)
  ) {
    error = payload.error ?? error;
    if (Array.isArray(payload.traceback)) {
      traceback = payload.traceback.map(String);
    }
  }
  const parsedRetry = parseRetryAfter(response.headers["retry-after"] ?? null);
  const retryAfter = parsedRetry === null ? null : Math.trunc(parsedRetry);
  return { error, traceback, retryAfter };
}

/** Incremental sans-io parser for `text/event-stream` bytes.
 *
 * Feed raw chunks, get complete frames `{id, event, data}` back.
 * Comment lines (`: keepalive`) are discarded per the SSE spec.
 */
export class SSEParser {
  // a single SSE line/frame has no business being this large; the
  // cap keeps a misbehaving peer from growing the buffer unbounded
  static MAX_BUFFER = 4 * 1024 * 1024;

  constructor() {
    this.decoder = new TextDecoder();
    this.buffer = "";
    this.event = null;
    this.eventId = null;
    this.dataLines = [];
    this.pendingSize = 0;
    this.dirty = false;
  }

  feed(chunk) {
    this.buffer += this.decoder.decode(chunk, { stream: true });
    // the cap covers BOTH the unterminated line and the frame
    // accumulated so far: many short data lines must not grow the
    // pending event unboundedly
    if (this.buffer.length + this.pendingSize > SSEParser.MAX_BUFFER) {
      throw new RangeError(
        `SSE frame exceeds ${SSEParser.MAX_BUFFER} bytes before completion`,
      );
    }
    const frames = [];
    for (;;) {
      const newline = this.buffer.indexOf("\n");
      if (newline < 0) {
        break;
      }
      let line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (line.endsWith("\r")) {
        line = line.slice(0, -1);
      }
      if (!line) {
        const frame = this.flush();
        if (frame !== null) {
          frames.push(frame);
        }
        continue;
      }
      if (line.startsWith(":")) {
        continue;
      }
      const colon = line.indexOf(":");
      const name = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) {
        value = value.slice(1);
      }
      this.dirty = true;
      if (name === "id") {
        const parsed = Number.parseInt(value, 10);
        this.eventId = Number.isNaN(parsed) ? null : parsed;
      } else if (name === "event") {
        this.event = value;
      } else if (name === "data") {
        this.dataLines.push(value);
        this.pendingSize += value.length;
      }
    }
    return frames;
  }

  /** Return the pending frame, if any, and reset the state. */
  flush() {
    if (!this.dirty) {
      return null;
    }
    const frame = {
      id: this.eventId,
      event: this.event,
      data: this.dataLines.join("\n"),
    };
    this.event = null;
    this.eventId = null;
    this.dataLines = [];
    this.pendingSize = 0;
    this.dirty = false;
    return frame;
  }
}

/** Decode an SSE frame's JSON payload. */
export function decodeFramePayload(frame, lastEventId = null) {
  if (frame.event === "sse_error") {
    throw Object.assign(new Error(frame.data), { lastEventId });
  }
  if (!frame.data) {
    return null;
  }
  const payload = JSON.parse(frame.data);
  if (
    payload === null ||
    typeof payload !== "object" ||
    Array.isArray(payload)
  ) {
    return null;
  }
  return payload;
}
