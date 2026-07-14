/** An offline test double with the exact generated client surface.
 *
 * Every API method is replaced by a wrapper that records the call and
 * returns the next mocked outcome for that operation. The last
 * outcome in a queue is sticky, so a single mock serves any number of
 * calls while a queue models state transitions (EXECUTING -> SUCCESS).
 */

import { ContreeClient as GeneratedClient } from "./client.js";
import { ContreeError } from "./errors.js";

export const RESERVED = new Set([
  "constructor",
  "request",
  "stream",
  "open",
  "close",
  "mock",
  "callsFor",
]);

const ASYNC_GENERATOR = async function* generatorProbe() {}.constructor;

function apiMethodNames() {
  return Object.getOwnPropertyNames(GeneratedClient.prototype).filter(
    (name) =>
      !RESERVED.has(name) &&
      !name.startsWith("_") &&
      typeof GeneratedClient.prototype[name] === "function",
  );
}

function unmocked(operation) {
  return new ContreeError(
    `no mock configured for ${operation}(); arm it with` +
      ` client.mock(${JSON.stringify(operation)}, result)`,
  );
}

export class ContreeClient extends GeneratedClient {
  constructor(token = "test-token", options = {}) {
    super(token, options);
    this.mocks = new Map();
    this.calls = [];
    // how the double was constructed, for assertions in tests of
    // code that builds clients itself (factories, fromProfile)
    this.constructedWith = {
      token,
      baseUrl: this.baseUrl,
      project: this.project,
      timeout: this.timeout,
      retry: this.retry,
      identity: this.identity,
    };
    for (const name of apiMethodNames()) {
      const method = GeneratedClient.prototype[name];
      this[name] =
        method instanceof ASYNC_GENERATOR
          ? this.iterWrapper(name)
          : this.callWrapper(name);
    }
  }

  record(operation, args) {
    this.calls.push({ operation, args });
    const queue = this.mocks.get(operation);
    if (queue === undefined || queue.length === 0) {
      throw unmocked(operation);
    }
    return queue.length > 1 ? queue.shift() : queue[0];
  }

  callWrapper(operation) {
    return async (...args) => {
      const outcome = this.record(operation, args);
      if (outcome.error !== null) {
        throw outcome.error;
      }
      return outcome.result;
    };
  }

  iterWrapper(operation) {
    const self = this;
    return async function* wrapper(...args) {
      const outcome = self.record(operation, args);
      yield* outcome.result ?? [];
      if (outcome.error !== null) {
        throw outcome.error;
      }
    };
  }

  /** Queue an outcome for *operation*; the last one queued is sticky. */
  mock(operation, result = null, { error = null } = {}) {
    if (typeof this[operation] !== "function" || RESERVED.has(operation)) {
      throw new ContreeError(
        `unknown API operation ${JSON.stringify(operation)}`,
      );
    }
    const queue = this.mocks.get(operation) ?? [];
    queue.push({ result, error });
    this.mocks.set(operation, queue);
    return this;
  }

  /** All recorded calls of *operation*, in order. */
  callsFor(operation) {
    return this.calls.filter((call) => call.operation === operation);
  }

  async request(spec) {
    throw unmocked(`${spec.method} ${spec.path}`);
  }

  // eslint-disable-next-line require-yield
  async *stream(spec) {
    throw unmocked(`${spec.method} ${spec.path}`);
  }

  async close() {}
}
