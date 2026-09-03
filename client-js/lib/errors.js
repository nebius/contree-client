/** Exception hierarchy for the Contree API client. */

export class ContreeError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = new.target.name;
  }
}

export class APIConnectionError extends ContreeError {
  constructor(message, { cause, timedOut = false } = {}) {
    super(message, { cause });
    this.timedOut = timedOut;
  }
}

export class APIStatusError extends ContreeError {
  constructor(status, error, { traceback = null, retryAfter = null } = {}) {
    super(`HTTP ${status}: ${error}`);
    this.status = status;
    this.error = error;
    this.traceback = traceback;
    this.retryAfter = retryAfter;
  }
}

export class BadRequestError extends APIStatusError {}
export class AuthenticationError extends APIStatusError {}
export class PermissionDeniedError extends APIStatusError {}
export class NotFoundError extends APIStatusError {}
export class ConflictError extends APIStatusError {}
export class GoneError extends APIStatusError {}
export class UnprocessableEntityError extends APIStatusError {}
export class TooEarlyError extends APIStatusError {}
export class RateLimitError extends APIStatusError {}
export class ServerError extends APIStatusError {}

export const ERROR_CLASSES = new Map([
  [400, BadRequestError],
  [401, AuthenticationError],
  [403, PermissionDeniedError],
  [404, NotFoundError],
  [409, ConflictError],
  [410, GoneError],
  [422, UnprocessableEntityError],
  [425, TooEarlyError],
  [429, RateLimitError],
]);
