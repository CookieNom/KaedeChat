import { describe, expect, it } from 'vitest';
import {
  isOperatorLegalConfig,
  resolveOperatorLegalConfig,
  type PublicBuildEnvironment
} from './public-build-config';

const completeEnvironment: PublicBuildEnvironment = {
  KAEDE_LEGAL_INSTANCE_NAME: 'Community Chat',
  KAEDE_LEGAL_OPERATOR_NAME: 'Community Cooperative',
  KAEDE_LEGAL_CONTACT_EMAIL: 'operator@community.test',
  KAEDE_LEGAL_EFFECTIVE_DATE: '2026-08-29',
  KAEDE_LEGAL_MINIMUM_AGE: '16',
  KAEDE_LEGAL_JURISDICTION: 'Example State'
};

describe('operator legal build configuration', () => {
  it('leaves an unconfigured default build in non-policy mode', () => {
    expect(resolveOperatorLegalConfig({}, 'default')).toBeNull();
  });

  it('fails a custom build when any required legal value is missing', () => {
    expect(() => resolveOperatorLegalConfig({}, 'custom')).toThrow(
      /missing KAEDE_LEGAL_INSTANCE_NAME/
    );
    expect(() =>
      resolveOperatorLegalConfig(
        { ...completeEnvironment, KAEDE_LEGAL_JURISDICTION: ' ' },
        'custom'
      )
    ).toThrow(/missing KAEDE_LEGAL_JURISDICTION/);
  });

  it('rejects partial policy data even on the default landing', () => {
    expect(() =>
      resolveOperatorLegalConfig(
        { KAEDE_LEGAL_CONTACT_EMAIL: 'operator@community.test' },
        'default'
      )
    ).toThrow(/complete operator legal configuration/);
  });

  it('normalizes and types complete operator copy', () => {
    const result = resolveOperatorLegalConfig(
      { ...completeEnvironment, KAEDE_LEGAL_INSTANCE_NAME: '  Community Chat  ' },
      'custom'
    );
    expect(result).toEqual({
      instanceName: 'Community Chat',
      operatorName: 'Community Cooperative',
      contactEmail: 'operator@community.test',
      effectiveDate: '2026-08-29',
      minimumAge: 16,
      jurisdiction: 'Example State'
    });
    expect(isOperatorLegalConfig(result)).toBe(true);
  });

  it.each([
    ['KAEDE_LEGAL_OPERATOR_NAME', '[Operator name]', /plain text/],
    ['KAEDE_LEGAL_OPERATOR_NAME', 'replace-with-operator', /plain text/],
    ['KAEDE_LEGAL_CONTACT_EMAIL', 'not-an-email', /valid non-placeholder email/],
    [
      'KAEDE_LEGAL_CONTACT_EMAIL',
      'replace-with-contact@example.test',
      /valid non-placeholder email/
    ],
    ['KAEDE_LEGAL_EFFECTIVE_DATE', '2026-02-30', /real date/],
    ['KAEDE_LEGAL_EFFECTIVE_DATE', '29 August 2026', /YYYY-MM-DD/],
    ['KAEDE_LEGAL_MINIMUM_AGE', '0', /1 through 120/],
    ['KAEDE_LEGAL_MINIMUM_AGE', '16.5', /1 through 120/]
  ] as const)('rejects unsafe %s values', (name, value, expected) => {
    expect(() =>
      resolveOperatorLegalConfig({ ...completeEnvironment, [name]: value }, 'custom')
    ).toThrow(expected);
  });
});
