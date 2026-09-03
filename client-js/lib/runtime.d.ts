export declare const CHUNK_SIZE: number;
export declare const PACKAGE_VERSION: string;
export declare const UA_PRODUCT: string;
export declare const IS_NODE: boolean;
export declare const UA_RUNTIME: string;
export declare const UA_PLATFORM: string;
export declare const RETRY_DELAYS: number[];
export declare const TIGHT_LOOP_FLOOR: number;

export interface ResponseData {
  status: number;
  headers: Record<string, string>;
  body: Uint8Array;
  /** the final URL after redirects, when the transport reports it */
  url?: string;
}

export type RequestBody =
  Uint8Array | string | Blob | ReadableStream<Uint8Array> | null;

export interface RequestSpec {
  method: string;
  path: string;
  query?: Record<string, string | readonly string[]>;
  headers?: Record<string, string>;
  body?: RequestBody;
  contentType?: string | null;
  accept?: string | null;
  idempotent?: boolean;
  redirect?: "manual" | "follow";
  /** absolute deadline in monotonic seconds; bounds transport waits */
  deadline?: number | null;
}

export declare function retryDelays(delays?: number[]): Generator<number>;

export declare class RetryPolicy {
  statuses: number[];
  serverErrors: boolean;
  delays: number[];
  maxAttempts: number | null;
  retryUnsafe: boolean;
  constructor(options?: {
    statuses?: number[];
    serverErrors?: boolean;
    delays?: number[];
    maxAttempts?: number | null;
    retryUnsafe?: boolean;
  });
  retryableStatus(status: number): boolean;
}

export declare function parseRetryAfter(
  value: string | null | undefined,
): number | null;
export declare function sleep(seconds: number): Promise<void>;
export declare function monotonic(): number;
export declare function isUuid(ref: string): boolean;
export declare function quotePath(value: unknown): string;
export declare function encodeQuery(
  query: Record<string, string | readonly string[]>,
): string;
export declare function formatTimeParam(value: string | number | Date): string;
export declare function parseDatetime(value: string): Date;
export declare function bytesToText(bytes: Uint8Array): string;
export declare function textToBytes(text: string): Uint8Array;
export declare function bytesToBase64(bytes: Uint8Array): string;
export declare function base64ToBytes(value: string): Uint8Array;
export declare function sha256(
  content: Uint8Array | string | Blob | ReadableStream<Uint8Array>,
): Promise<string | null>;
export declare function jsonBody(response: ResponseData): unknown;
export declare function jsonObject(
  response: ResponseData,
): Record<string, unknown>;
export declare function jsonArray(response: ResponseData): unknown[];
export declare function responseErrorDetails(response: ResponseData): {
  error: unknown;
  traceback: string[] | null;
  retryAfter: number | null;
};

export interface SSEFrame {
  id: number | null;
  event: string | null;
  data: string;
}

export declare class SSEParser {
  static MAX_BUFFER: number;
  feed(chunk: Uint8Array): SSEFrame[];
  flush(): SSEFrame | null;
}

export declare function decodeFramePayload(
  frame: SSEFrame,
  lastEventId?: number | null,
): Record<string, unknown> | null;
