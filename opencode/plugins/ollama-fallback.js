// Before each user message on an Ollama model, check that the Ollama server is
// reachable and serves the requested model. If not, switch that message to
// FALLBACK. This is a preflight only: a failure mid-generation is not retried.

const FALLBACK = { providerID: "openai", modelID: "gpt-6-astra" }
const TIMEOUT_MS = 1500
const CACHE_MS = 15000

export const OllamaFallback = async ({ client }) => {
  let baseURL = "http://127.0.0.1:11434/v1"
  let cache = { at: 0, models: null }

  // Returns the set of model ids Ollama serves, or null if unreachable.
  async function listModels() {
    if (Date.now() - cache.at < CACHE_MS) return cache.models
    let models = null
    try {
      const res = await fetch(`${baseURL.replace(/\/+$/, "")}/models`, {
        signal: AbortSignal.timeout(TIMEOUT_MS),
      })
      if (res.ok) models = new Set(((await res.json()).data ?? []).map((m) => m.id))
    } catch {}
    cache = { at: Date.now(), models }
    return models
  }

  return {
    config: async (config) => {
      baseURL = config.provider?.ollama?.options?.baseURL ?? baseURL
    },
    "chat.message": async (_input, output) => {
      const model = output.message.model
      if (model?.providerID !== "ollama") return

      const models = await listModels()
      if (models?.has(model.modelID)) return

      const reason = models ? "model not served" : "server unreachable"
      output.message.model = { ...FALLBACK }
      await client.app
        .log({
          body: {
            service: "ollama-fallback",
            level: "warn",
            message: `ollama/${model.modelID} unavailable (${reason}); using ${FALLBACK.providerID}/${FALLBACK.modelID}`,
            extra: { baseURL, agent: output.message.agent },
          },
        })
        .catch(() => {})
    },
  }
}
