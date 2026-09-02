/** Exception hierarchy for the Contree API client. */

export class ContreeError extends Error {
  constructor(message) {
    super(message);
    this.name = new.target.name;
  }
}

export class ContreeTransportError extends ContreeError {}
export class ContreeStreamError extends ContreeTransportError {}

export class SSEStreamError extends ContreeStreamError {
  constructor(message, { lastEventId = null } = {}) {
    super(message);
    this.lastEventId = lastEventId;
  }
}

export class ContreeHTTPError extends ContreeTransportError {}

export class ContreeAPIError extends ContreeHTTPError {
  constructor(status, error, { traceback = null, retryAfter = null } = {}) {
    super(`HTTP ${status}: ${error}`);
    this.status = status;
    this.error = error;
    this.traceback = traceback;
    this.retryAfter = retryAfter;
  }
}

export class BadRequestError extends ContreeAPIError {}
export class UnauthorizedError extends ContreeAPIError {}
export class ForbiddenError extends ContreeAPIError {}
export class NotFoundError extends ContreeAPIError {}
export class ConflictError extends ContreeAPIError {}
export class GoneError extends ContreeAPIError {}
export class UnprocessableEntityError extends ContreeAPIError {}
export class TooEarlyError extends ContreeAPIError {}
export class ServerError extends ContreeAPIError {}

export const ERROR_CLASSES = new Map([
  [400, BadRequestError],
  [401, UnauthorizedError],
  [403, ForbiddenError],
  [404, NotFoundError],
  [409, ConflictError],
  [410, GoneError],
  [422, UnprocessableEntityError],
  [425, TooEarlyError],
]);
