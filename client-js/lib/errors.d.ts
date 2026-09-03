export declare class ContreeError extends Error {
  constructor(message: string, options?: ErrorOptions);
}

export declare class APIConnectionError extends ContreeError {
  timedOut: boolean;
  constructor(
    message: string,
    options?: { cause?: unknown; timedOut?: boolean },
  );
}

export declare class APIStatusError extends ContreeError {
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

export declare class BadRequestError extends APIStatusError {}
export declare class AuthenticationError extends APIStatusError {}
export declare class PermissionDeniedError extends APIStatusError {}
export declare class NotFoundError extends APIStatusError {}
export declare class ConflictError extends APIStatusError {}
export declare class GoneError extends APIStatusError {}
export declare class UnprocessableEntityError extends APIStatusError {}
export declare class TooEarlyError extends APIStatusError {}
export declare class RateLimitError extends APIStatusError {}
export declare class ServerError extends APIStatusError {}

export declare const ERROR_CLASSES: Map<number, typeof APIStatusError>;
