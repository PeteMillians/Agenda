// What the UI calls each calendar provider.
export type Provider = "google" | "microsoft";

export const PROVIDER_NAMES: Record<Provider, string> = { google: "Google", microsoft: "Outlook" };
