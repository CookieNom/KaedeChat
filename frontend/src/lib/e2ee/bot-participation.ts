export type BotE2eeDeviceStatus = 'pending' | 'active' | 'revoked';

export interface BotE2eeParticipationDevice {
  device_id: string;
  status: BotE2eeDeviceStatus;
  consent_generation: string;
  joined_epoch: string;
  history_floor_message_ref: string | null;
}

export interface BotE2eeParticipation {
  application_ref: string;
  channel_ref: string;
  e2ee_mode: 'participant';
  devices: BotE2eeParticipationDevice[];
}

export interface DmBotE2eeParticipantConsent {
  user_ref: string;
  consented: boolean;
}

export interface DmBotE2eeParticipation {
  application_ref: string;
  channel_ref: string;
  consent_state: 'pending' | 'active' | 'revoked';
  consent_generation: string;
  history_floor_message_ref: string | null;
  participants: DmBotE2eeParticipantConsent[];
  devices: Array<{
    device_id: string;
    status: BotE2eeDeviceStatus;
    joined_epoch: string;
  }>;
  encryption_policy: Record<string, unknown>;
}

export function botE2eeParticipationPath(
  guildRef: string,
  channelRef: string,
  applicationRef: string
): string {
  return `/guilds/${encodeURIComponent(guildRef)}/channels/${encodeURIComponent(channelRef)}/e2ee/bots/${encodeURIComponent(applicationRef)}`;
}

export function dmBotE2eeParticipationPath(channelRef: string, applicationRef: string): string {
  return `/channels/${encodeURIComponent(channelRef)}/e2ee/bots/${encodeURIComponent(applicationRef)}`;
}

export function botE2eeHistoryNotice(device: BotE2eeParticipationDevice): string {
  return device.history_floor_message_ref
    ? `No access before message ${device.history_floor_message_ref}`
    : 'No access to messages sent before consent';
}
