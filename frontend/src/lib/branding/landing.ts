import {
  isOperatorLegalConfig,
  normalizeLandingVariant,
  type LandingPageVariant,
  type OperatorLegalConfig
} from './public-build-config';

export { normalizeLandingVariant, type LandingPageVariant } from './public-build-config';

const raw: unknown = import.meta.env.KAEDE_LANDING_PAGE;

export const landingPageVariant: LandingPageVariant = normalizeLandingVariant(raw);

/** True when the operator-facing landing page is active for this build. */
export const isCustomLanding: boolean = landingPageVariant === 'custom';

const rawOperatorLegalConfig: unknown = import.meta.env.KAEDE_OPERATOR_LEGAL_CONFIG;

/** Operator-authored legal copy, or null on an unconfigured default build. */
export const operatorLegalConfig: OperatorLegalConfig | null = isOperatorLegalConfig(
  rawOperatorLegalConfig
)
  ? rawOperatorLegalConfig
  : null;
