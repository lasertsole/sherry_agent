import type { FetchError } from 'ofetch';

export type Response = {
  code?: number;
  data?: unknown;
  msg?: string;
};

export type UseFetchResponse = {
  data: Ref<unknown>;
  error: Ref<FetchError<unknown> | null, FetchError<unknown> | null>;
  status: Ref<string>;
  refresh: () => Promise<void>;
  clear: () => void;
};
