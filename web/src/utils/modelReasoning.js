export function reasoningCapabilityForModel(model, capabilities = []) {
  const normalizedModel = String(model || '').trim().toLowerCase()
  if (!normalizedModel) return null
  return capabilities.find(capability => {
    try {
      return new RegExp(String(capability?.model_pattern || '')).test(normalizedModel)
    } catch {
      return false
    }
  }) || null
}

export function commonReasoningEfforts(capabilities = []) {
  return [...new Set(
    capabilities.flatMap(capability => capability?.accepted_efforts || []),
  )]
}
