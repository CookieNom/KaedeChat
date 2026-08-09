export interface NativePlatformInfo {
  native: true;
  os: string;
  arch: string;
  native_voice: boolean;
  native_notifications: boolean;
  secure_credentials: boolean;
}

export interface NativeResponse<T = unknown> {
  status: number;
  body: T;
  headers: Record<string, string>;
}

export interface NativeError {
  code?: string;
  message?: string;
  status?: number;
  detail?: Record<string, unknown>;
}

export interface NativeAudioDevice {
  id: string;
  label: string;
  is_default: boolean;
  channels: number;
  sample_rate: number;
}

export interface NativeDevicePreference {
  id: string;
  label: string;
}

export interface NativePreferences {
  input_device: NativeDevicePreference | null;
  output_device: NativeDevicePreference | null;
  camera_device: NativeDevicePreference | null;
  screen_source: NativeDevicePreference | null;
  input_mode: 'push_to_talk' | 'voice_activity';
  vad_threshold: number;
  push_to_talk_hotkey: string | null;
  noise_suppression: 'off' | 'standard' | 'voice_isolation';
  echo_cancellation: boolean;
  automatic_gain_control: boolean;
}

export interface NativeDevices {
  inputs: NativeAudioDevice[];
  outputs: NativeAudioDevice[];
  cameras: { id: string; label: string }[];
  screens: { id: string; label: string }[];
}

export interface NativeVoiceStatus {
  state: 'disconnected' | 'connecting' | 'connected' | 'reconnecting' | 'media_error' | 'failed';
  message?: string;
  room?: string;
  can_speak?: boolean;
  can_stream?: boolean;
  screen?: boolean;
  camera?: boolean;
  muted?: boolean;
  deafened?: boolean;
  input_level?: number;
}

interface TauriGlobal {
  core: {
    invoke<T>(command: string, args?: unknown): Promise<T>;
  };
}

declare global {
  interface Window {
    __TAURI__?: TauriGlobal;
  }
}

export function isNativeDesktop(): boolean {
  return typeof window !== 'undefined' && typeof window.__TAURI__?.core.invoke === 'function';
}

export async function nativeInvoke<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  const invoke = window.__TAURI__?.core.invoke;
  if (!invoke) throw new Error('Native desktop bridge is unavailable.');
  return invoke<T>(command, args);
}

export async function nativeInvokeBytes<T>(command: string, body: Uint8Array): Promise<T> {
  const invoke = window.__TAURI__?.core.invoke;
  if (!invoke) throw new Error('Native desktop bridge is unavailable.');
  return invoke<T>(command, body);
}

export function storedNativeInstance(): string {
  if (typeof localStorage === 'undefined') return '';
  return localStorage.getItem('kaede.native.instance') ?? '';
}

export async function setNativeInstance(instance: string): Promise<string> {
  const normalized = await nativeInvoke<string>('native_set_instance', { instance });
  localStorage.setItem('kaede.native.instance', normalized);
  return normalized;
}

export async function initializeNativeInstance(): Promise<void> {
  if (!isNativeDesktop()) return;
  const instance = storedNativeInstance();
  if (instance) await setNativeInstance(instance);
}

export function nativeError(value: unknown): NativeError {
  if (typeof value === 'object' && value !== null) return value as NativeError;
  return { message: value instanceof Error ? value.message : String(value) };
}
