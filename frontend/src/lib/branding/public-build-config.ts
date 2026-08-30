export type LandingPageVariant = 'default' | 'custom';

export const operatorLegalEnvironmentNames = [
  'KAEDE_LEGAL_INSTANCE_NAME',
  'KAEDE_LEGAL_OPERATOR_NAME',
  'KAEDE_LEGAL_CONTACT_EMAIL',
  'KAEDE_LEGAL_EFFECTIVE_DATE',
  'KAEDE_LEGAL_MINIMUM_AGE',
  'KAEDE_LEGAL_JURISDICTION'
] as const;

export type OperatorLegalEnvironmentName = (typeof operatorLegalEnvironmentNames)[number];
export type PublicBuildEnvironment = Partial<Record<OperatorLegalEnvironmentName, string>>;

export interface OperatorLegalConfig {
  instanceName: string;
  operatorName: string;
  contactEmail: string;
  effectiveDate: string;
  minimumAge: number;
  jurisdiction: string;
}

const textFields = [
  ['KAEDE_LEGAL_INSTANCE_NAME', 'instanceName', 200],
  ['KAEDE_LEGAL_OPERATOR_NAME', 'operatorName', 300],
  ['KAEDE_LEGAL_JURISDICTION', 'jurisdiction', 300]
] as const;

const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const isoDatePattern = /^(\d{4})-(\d{2})-(\d{2})$/;
const placeholderPattern = /change-me|changeme|not-configured|not-used|replace[-_]/i;

export function normalizeLandingVariant(raw: unknown): LandingPageVariant {
  return typeof raw === 'string' && raw.trim().toLowerCase() === 'custom' ? 'custom' : 'default';
}

function readValue(
  environment: PublicBuildEnvironment,
  name: OperatorLegalEnvironmentName
): string {
  return environment[name]?.trim() ?? '';
}

function isValidIsoDate(value: string): boolean {
  const match = isoDatePattern.exec(value);
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(Date.UTC(year, month - 1, day));
  return (
    date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day
  );
}

function hasControlCharacter(value: string): boolean {
  return [...value].some((character) => {
    const codePoint = character.codePointAt(0) ?? 0;
    return codePoint < 32 || codePoint === 127;
  });
}

function requirePlainText(
  name: OperatorLegalEnvironmentName,
  value: string,
  maximum: number
): void {
  if (
    value.length > maximum ||
    hasControlCharacter(value) ||
    value.includes('[') ||
    value.includes(']') ||
    placeholderPattern.test(value)
  ) {
    throw new Error(
      `${name} must be non-placeholder plain text no longer than ${maximum} characters`
    );
  }
}

/**
 * Resolve the operator-authored legal copy embedded into a frontend build.
 *
 * The default project landing deliberately works without operator legal data;
 * its legal routes then render a non-policy notice. A custom landing, or a
 * partially configured policy under either variant, fails the build.
 */
export function resolveOperatorLegalConfig(
  environment: PublicBuildEnvironment,
  landingPage: LandingPageVariant
): OperatorLegalConfig | null {
  const values = Object.fromEntries(
    operatorLegalEnvironmentNames.map((name) => [name, readValue(environment, name)])
  ) as Record<OperatorLegalEnvironmentName, string>;
  const configured = operatorLegalEnvironmentNames.filter((name) => values[name]);
  const missing = operatorLegalEnvironmentNames.filter((name) => !values[name]);

  if (configured.length === 0 && landingPage === 'default') return null;
  if (missing.length > 0) {
    throw new Error(
      `KAEDE_LANDING_PAGE=${landingPage} requires a complete operator legal configuration; missing ${missing.join(', ')}`
    );
  }

  for (const [environmentName, , maximum] of textFields) {
    requirePlainText(environmentName, values[environmentName], maximum);
  }

  const contactEmail = values.KAEDE_LEGAL_CONTACT_EMAIL;
  if (
    contactEmail.length > 254 ||
    !emailPattern.test(contactEmail) ||
    hasControlCharacter(contactEmail) ||
    placeholderPattern.test(contactEmail)
  ) {
    throw new Error('KAEDE_LEGAL_CONTACT_EMAIL must be a valid non-placeholder email address');
  }

  const effectiveDate = values.KAEDE_LEGAL_EFFECTIVE_DATE;
  if (!isValidIsoDate(effectiveDate)) {
    throw new Error('KAEDE_LEGAL_EFFECTIVE_DATE must be a real date in YYYY-MM-DD format');
  }

  const minimumAge = values.KAEDE_LEGAL_MINIMUM_AGE;
  if (!/^\d{1,3}$/.test(minimumAge) || Number(minimumAge) < 1 || Number(minimumAge) > 120) {
    throw new Error('KAEDE_LEGAL_MINIMUM_AGE must be an integer from 1 through 120');
  }

  return {
    instanceName: values.KAEDE_LEGAL_INSTANCE_NAME,
    operatorName: values.KAEDE_LEGAL_OPERATOR_NAME,
    contactEmail,
    effectiveDate,
    minimumAge: Number(minimumAge),
    jurisdiction: values.KAEDE_LEGAL_JURISDICTION
  };
}

export function isOperatorLegalConfig(value: unknown): value is OperatorLegalConfig {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Partial<OperatorLegalConfig>;
  return (
    typeof candidate.instanceName === 'string' &&
    typeof candidate.operatorName === 'string' &&
    typeof candidate.contactEmail === 'string' &&
    typeof candidate.effectiveDate === 'string' &&
    typeof candidate.minimumAge === 'number' &&
    Number.isInteger(candidate.minimumAge) &&
    typeof candidate.jurisdiction === 'string'
  );
}
