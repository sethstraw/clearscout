import type {
  GuidedCampaignConfiguration,
  Provider,
  RelevanceReviewConfiguration,
  SearchVocabularyConfiguration,
} from "./client";

/** Vendors capitalize their own names; title-casing the enum gets them wrong. */
const PROVIDER_NAMES: Record<Provider["provider"], string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
};

export function providerName(provider: Provider["provider"]): string {
  return PROVIDER_NAMES[provider];
}

function modelChoiceDescription(
  provider: Provider["provider"] | undefined,
  modelChoice: { model: string; effort: string | null; service_tier: string } | undefined,
  label: string,
): string {
  if (!provider || !modelChoice) return "";
  const name = PROVIDER_NAMES[provider];
  if (!name || !modelChoice.model.trim()) return "";
  const effort = modelChoice.effort ?? "provider default";
  const tier = modelChoice.service_tier === "flex" ? "Flex" : "standard";
  return `Configured provider: ${name}. ${label}: ${modelChoice.model}. Effort: ${effort}. ${tier} tier.`;
}

export function guidedCampaignConfigurationDescription(
  configuration: GuidedCampaignConfiguration | undefined,
): string {
  return modelChoiceDescription(
    configuration?.provider,
    configuration?.model_choice,
    "Search campaign",
  );
}

export function searchVocabularyConfigurationDescription(
  configuration: SearchVocabularyConfiguration | undefined,
): string {
  return modelChoiceDescription(
    configuration?.provider,
    configuration?.model_choice,
    "Search vocabulary",
  );
}

export function relevanceReviewConfigurationDescription(
  configuration: RelevanceReviewConfiguration | undefined,
): string {
  return modelChoiceDescription(
    configuration?.provider,
    configuration?.model_choice,
    "Relevance review",
  );
}
