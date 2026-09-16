export interface OnboardingOption {
  id: string;
  title: string;
  description: string;
  emoji: string;
  role_ids: string[];
  channel_ids: string[];
}
export interface OnboardingQuestion {
  id: string;
  title: string;
  description: string;
  required: boolean;
  multiple: boolean;
  before_join: boolean;
  options: OnboardingOption[];
}
export interface OnboardingConfig {
  enabled: boolean;
  revision: number;
  rules_revision: number;
  welcome: string;
  rules: string[];
  default_channel_ids: string[];
  questions: OnboardingQuestion[];
  guide: {
    id: string;
    title: string;
    description: string;
    channel_id: string | null;
    kind: 'task' | 'resource';
  }[];
}
export interface OnboardingResponse {
  config: OnboardingConfig;
  state: {
    answers?: Record<string, string[]>;
    completed_tasks?: string[];
    extra_channel_ids?: string[];
    channel_ids?: string[];
    completed_at?: string;
  };
  can_manage: boolean;
  needs_rules: boolean;
  needs_onboarding: boolean;
}
export function emptyOnboarding(): OnboardingConfig {
  return {
    enabled: false,
    revision: 0,
    rules_revision: 0,
    welcome: 'Welcome! Make yourself at home.',
    rules: [],
    default_channel_ids: [],
    questions: [],
    guide: []
  };
}
