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
  screen_share_profile: 'data_saver' | 'smooth' | 'sharp' | 'source';
  audio_quality: 'data_saver' | 'standard' | 'high' | 'studio';
  share_system_audio: boolean;
}

export interface NativeDevices {
  inputs: NativeAudioDevice[];
  outputs: NativeAudioDevice[];
  cameras: { id: string; label: string }[];
  screens: NativeScreenSource[];
}

export interface NativeScreenSource {
  id: string;
  label: string;
  kind?: 'application' | 'screen';
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

export interface NativeSessionBootstrap {
  instance: string | null;
  authenticated: boolean;
}

export interface NativeUpdateStatus {
  current_version: string;
  supported: boolean;
  support_message: string | null;
  available: boolean;
  version: string | null;
  notes: string | null;
  published_at: string | null;
}

export interface NativeTaskbarPinStatus {
  supported: boolean;
  allowed: boolean;
  pinned: boolean;
}

export interface NativeAutostartStatus {
  enabled: boolean;
}

let nativeInitialization: Promise<NativeSessionBootstrap> | null = null;
const LAST_NATIVE_ROUTE = 'kaede.native.last-route';

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

export function rememberNativeRoute(value: string): void {
  if (!isNativeDesktop() || typeof localStorage === 'undefined') return;
  const route = safeNativeRoute(value);
  if (route) localStorage.setItem(LAST_NATIVE_ROUTE, route);
}

export function storedNativeRoute(): string | null {
  if (typeof localStorage === 'undefined' || typeof window === 'undefined') return null;
  return safeNativeRoute(localStorage.getItem(LAST_NATIVE_ROUTE) ?? '');
}

function safeNativeRoute(value: string): string | null {
  if (!value.startsWith('/') || value.startsWith('//') || value.length > 2048) return null;
  try {
    const url = new URL(value, 'https://desktop.kaede.invalid');
    if (url.origin !== 'https://desktop.kaede.invalid') return null;
    if (
      !/^\/(?:home(?:\/|$)|g\/|settings(?:\/|$)|developers(?:\/|$)|administration(?:\/|$)|applications\/|invite\/)/.test(
        url.pathname
      )
    )
      return null;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return null;
  }
}

export async function setNativeInstance(instance: string): Promise<string> {
  const normalized = await nativeInvoke<string>('native_set_instance', { instance });
  localStorage.setItem('kaede.native.instance', normalized);
  return normalized;
}

async function restoreNativeSession(): Promise<NativeSessionBootstrap> {
  const preferredInstance = storedNativeInstance();
  if (preferredInstance) await setNativeInstance(preferredInstance);

  // The account registry and credentials live outside the WebView. Always ask
  // Rust for the authenticated state, even when the WebView remembered an
  // instance, so a process restart cannot race protected API requests.
  const restored = await nativeInvoke<NativeSessionBootstrap>('native_restore_session');
  if (restored.instance) localStorage.setItem('kaede.native.instance', restored.instance);
  return restored;
}

export function initializeNativeInstance(): Promise<NativeSessionBootstrap> {
  if (!isNativeDesktop()) {
    return Promise.resolve({ instance: null, authenticated: false });
  }
  if (!nativeInitialization) {
    nativeInitialization = restoreNativeSession().catch((error) => {
      // Native storage can be temporarily unavailable while the OS unlocks a
      // credential vault. Permit the next request to retry instead of caching
      // a failed startup for the lifetime of the application.
      nativeInitialization = null;
      throw error;
    });
  }
  return nativeInitialization;
}

export function nativeError(value: unknown): NativeError {
  if (typeof value === 'object' && value !== null) return value as NativeError;
  return { message: value instanceof Error ? value.message : String(value) };
}
