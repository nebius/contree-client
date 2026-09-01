export declare class ContreeError extends Error {}

export declare class ContreeTransportError extends ContreeError {}
export declare class ContreeProtocolError extends ContreeTransportError {}
export declare class ContreeStreamError extends ContreeProtocolError {}

export declare class SSEStreamError extends ContreeStreamError {
  lastEventId: number | null;
  constructor(message: string, options?: { lastEventId?: number | null });
}

export declare class ContreeHTTPError extends ContreeTransportError {}

export declare class ContreeAPIError extends ContreeHTTPError {
  status: number;
  error: unknown;
  traceback: string[] | null;
  retryAfter: number | null;
  constructor(
    status: number,
    error: unknown,
    options?: { traceback?: string[] | null; retryAfter?: number | null },
  );
}

export declare class BadRequestError extends ContreeAPIError {}
export declare class UnauthorizedError extends ContreeAPIError {}
export declare class ForbiddenError extends ContreeAPIError {}
export declare class NotFoundError extends ContreeAPIError {}
export declare class ConflictError extends ContreeAPIError {}
export declare class GoneError extends ContreeAPIError {}
export declare class UnprocessableEntityError extends ContreeAPIError {}
export declare class TooEarlyError extends ContreeAPIError {}
export declare class ServerError extends ContreeAPIError {}

export declare const ERROR_CLASSES: Map<number, typeof ContreeAPIError>;
