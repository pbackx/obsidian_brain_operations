/**
 * Pi extension for per-task OpenRouter upstream provider routing.
 *
 * Reads comma-separated OpenRouter provider slugs from `PI_OPENROUTER_PROVIDER`
 * (e.g. "nebius" or "nebius,mistral") and injects `provider.only` +
 * `provider.allow_fallbacks: false` into OpenRouter request payloads so
 * requests run strictly through the chosen upstream provider(s).
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  const raw = process.env.PI_OPENROUTER_PROVIDER?.trim();
  if (!raw) return;

  const providers = raw
    .split(",")
    .map((slug) => slug.trim())
    .filter(Boolean);
  if (providers.length === 0) return;

  pi.on("before_provider_request", (event, ctx) => {
    if (ctx.model?.provider !== "openrouter") return undefined;
    if (!event.payload || typeof event.payload !== "object") return undefined;

    const payload = event.payload as Record<string, unknown>;
    const existingProvider =
      payload.provider && typeof payload.provider === "object"
        ? (payload.provider as Record<string, unknown>)
        : {};

    return {
      ...payload,
      provider: {
        ...existingProvider,
        only: providers,
        allow_fallbacks: false,
      },
    };
  });
}
