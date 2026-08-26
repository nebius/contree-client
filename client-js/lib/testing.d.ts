import { ContreeClient as GeneratedClient } from "./client.js";
import type { RetryPolicy } from "./runtime.js";

export declare const RESERVED: Set<string>;

export interface Outcome {
  result: unknown;
  error: unknown;
}

export interface Call {
  operation: string;
  args: unknown[];
}

export declare class ContreeClient extends GeneratedClient {
  mocks: Map<string, Outcome[]>;
  calls: Call[];
  constructedWith: {
    token: string | null;
    baseUrl: string;
    project: string | null;
    timeout: number | null;
    retry: RetryPolicy | null;
    identity: string | null;
  };
  constructor(
    token?: string | null,
    options?: ConstructorParameters<typeof GeneratedClient>[1],
  );
  mock(
    operation: string,
    result?: unknown,
    options?: { error?: unknown },
  ): this;
  callsFor(operation: string): Call[];
}
