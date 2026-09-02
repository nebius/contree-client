import assert from "node:assert/strict";
import { test } from "node:test";

import {
  encodeQuery,
  parseRetryAfter,
  RETRY_DELAYS,
  RetryPolicy,
  SSEParser,
} from "../lib/runtime.js";
import {
  ContreeError,
  ContreeStreamError,
  ContreeTransportError,
  SSEStreamError,
} from "../lib/errors.js";

test("stream errors retain the transport hierarchy", () => {
  const error = new SSEStreamError("stream failed");
  assert.ok(error instanceof ContreeStreamError);
  assert.ok(error instanceof ContreeTransportError);
  assert.ok(error instanceof ContreeError);
});

test("encodeQuery preserves repeated values", () => {
  assert.equal(
    encodeQuery({
      pattern: ["^root:", "^bin:"],
      path: ["/etc/passwd", "/etc/group"],
      case: "sensitive",
    }),
    "pattern=%5Eroot%3A&pattern=%5Ebin%3A" +
      "&path=/etc/passwd&path=/etc/group&case=sensitive",
  );
});

test("parseRetryAfter: delta seconds, dates, garbage and infinities", () => {
  assert.equal(parseRetryAfter("7"), 7);
  assert.equal(parseRetryAfter("0"), 0);
  assert.equal(parseRetryAfter("-5"), 0);
  assert.equal(parseRetryAfter("soon"), null);
  assert.equal(parseRetryAfter(null), null);
  // Number() parses these, but an infinite sleep must never happen
  assert.equal(parseRetryAfter("Infinity"), null);
  assert.equal(parseRetryAfter("-Infinity"), null);
  assert.equal(parseRetryAfter("NaN"), null);
  const future = new Date(Date.now() + 30_000).toUTCString();
  const delay = parseRetryAfter(future);
  assert.ok(delay !== null && delay > 25 && delay < 31);
  const past = new Date(Date.now() - 30_000).toUTCString();
  assert.equal(parseRetryAfter(past), 0);
});

test("RetryPolicy validates its configuration", () => {
  assert.throws(() => new RetryPolicy({ delays: [] }), RangeError);
  assert.throws(() => new RetryPolicy({ delays: [Infinity] }), RangeError);
  assert.throws(() => new RetryPolicy({ delays: [NaN] }), RangeError);
  assert.throws(() => new RetryPolicy({ delays: [-1] }), RangeError);
  assert.throws(() => new RetryPolicy({ maxAttempts: 0 }), RangeError);
  assert.throws(() => new RetryPolicy({ maxAttempts: 2.5 }), RangeError);
  assert.throws(() => new RetryPolicy({ maxAttempts: NaN }), RangeError);
  new RetryPolicy({ delays: [0, 1], maxAttempts: 1 }); // valid
});

test("RetryPolicy and RETRY_DELAYS are immutable", () => {
  const source = [0.5];
  const policy = new RetryPolicy({ delays: source, statuses: [410] });
  source.push(999); // mutating the input must not affect the policy
  assert.deepEqual([...policy.delays], [0.5]);
  assert.throws(() => policy.delays.push(1), TypeError);
  assert.throws(() => {
    policy.maxAttempts = null;
  }, TypeError);
  assert.throws(() => RETRY_DELAYS.push(99), TypeError);
});

test("SSEParser caps the accumulated pending event, not just a line", () => {
  const parser = new SSEParser();
  // many short, individually valid data lines must not grow the
  // pending frame unboundedly
  const line = `data: ${"x".repeat(1024)}\n`;
  const chunk = new TextEncoder().encode(line.repeat(64));
  assert.throws(() => {
    for (let i = 0; i < 100; i += 1) {
      parser.feed(chunk);
    }
  }, SSEStreamError);
});
