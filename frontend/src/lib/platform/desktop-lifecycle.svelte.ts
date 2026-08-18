import {
  isNativeDesktop,
  nativeError,
  nativeInvoke,
  type NativeAutostartStatus,
  type NativePlatformInfo,
  type NativeTaskbarPinStatus,
  type NativeUpdateStatus
} from './native';

const TASKBAR_PROMPT_HANDLED = 'kaede.native.taskbar-prompt-handled';
const UPDATE_DISMISSED_VERSION = 'kaede.native.update-dismissed-version';
export const NATIVE_UPDATE_POLL_INTERVAL_MS = 6 * 60 * 60 * 1000;

function errorMessage(caught: unknown, fallback: string): string {
  const parsed = nativeError(caught);
  return typeof parsed.message === 'string' && parsed.message.trim() ? parsed.message : fallback;
}

class DesktopLifecycle {
  update = $state<NativeUpdateStatus | null>(null);
  taskbar = $state<NativeTaskbarPinStatus | null>(null);
  autostart = $state<NativeAutostartStatus | null>(null);
  checking = $state(false);
  installing = $state(false);
  pinning = $state(false);
  savingAutostart = $state(false);
  updateError = $state('');
  pinError = $state('');
  autostartError = $state('');
  checkedAt = $state<Date | null>(null);
  showTaskbarPrompt = $state(false);
  dismissedUpdateVersion = $state('');

  async initialize(): Promise<void> {
    if (!isNativeDesktop()) return;
    try {
      this.dismissedUpdateVersion = localStorage.getItem(UPDATE_DISMISSED_VERSION) ?? '';
    } catch {
      this.dismissedUpdateVersion = '';
    }
    await Promise.allSettled([
      this.checkForUpdates(false),
      this.refreshTaskbarStatus(true),
      this.refreshAutostartStatus()
    ]);
  }

  async refreshAutostartStatus(): Promise<void> {
    if (!isNativeDesktop()) return;
    this.autostartError = '';
    try {
      this.autostart = await nativeInvoke<NativeAutostartStatus>('native_autostart_status');
    } catch (caught) {
      this.autostartError = errorMessage(
        caught,
        'Kaede could not read the launch-at-sign-in setting.'
      );
    }
  }

  async setAutostart(enabled: boolean): Promise<void> {
    if (!isNativeDesktop() || this.savingAutostart) return;
    this.savingAutostart = true;
    this.autostartError = '';
    try {
      this.autostart = await nativeInvoke<NativeAutostartStatus>('native_autostart_set', {
        enabled
      });
    } catch (caught) {
      this.autostartError = errorMessage(
        caught,
        'Kaede could not update the launch-at-sign-in setting.'
      );
    } finally {
      this.savingAutostart = false;
    }
  }

  async checkForUpdates(reportErrors = true): Promise<void> {
    if (!isNativeDesktop() || this.checking || this.installing) return;
    this.checking = true;
    if (reportErrors) this.updateError = '';
    try {
      this.update = await nativeInvoke<NativeUpdateStatus>('native_update_check');
      this.checkedAt = new Date();
      if (this.update.version !== this.dismissedUpdateVersion) this.dismissedUpdateVersion = '';
    } catch (caught) {
      if (reportErrors) {
        this.updateError = errorMessage(caught, 'Kaede could not check for updates.');
      }
    } finally {
      this.checking = false;
    }
  }

  async installUpdate(): Promise<void> {
    if (!isNativeDesktop() || this.installing) return;
    this.installing = true;
    this.updateError = '';
    try {
      await nativeInvoke<void>('native_update_install');
    } catch (caught) {
      this.updateError = errorMessage(caught, 'Kaede could not install the update.');
      this.installing = false;
    }
  }

  dismissUpdate(): void {
    const version = this.update?.version;
    if (!version) return;
    this.dismissedUpdateVersion = version;
    try {
      localStorage.setItem(UPDATE_DISMISSED_VERSION, version);
    } catch {
      // Dismissal only needs to last for this process when storage is unavailable.
    }
  }

  async refreshTaskbarStatus(offerIfEligible = false): Promise<void> {
    if (!isNativeDesktop()) return;
    try {
      const platform = await nativeInvoke<NativePlatformInfo>('native_platform_info');
      if (platform.os !== 'windows') return;
      this.taskbar = await nativeInvoke<NativeTaskbarPinStatus>('native_taskbar_pin_status');
      const handled = (() => {
        try {
          return localStorage.getItem(TASKBAR_PROMPT_HANDLED) === 'true';
        } catch {
          return false;
        }
      })();
      this.showTaskbarPrompt =
        offerIfEligible &&
        !handled &&
        this.taskbar.supported &&
        this.taskbar.allowed &&
        !this.taskbar.pinned;
    } catch {
      // Older Windows builds and portable copies may not expose TaskbarManager.
      this.taskbar = { supported: false, allowed: false, pinned: false };
    }
  }

  dismissTaskbarPrompt(): void {
    this.showTaskbarPrompt = false;
    try {
      localStorage.setItem(TASKBAR_PROMPT_HANDLED, 'true');
    } catch {
      // The prompt remains dismissed for this process.
    }
  }

  async requestTaskbarPin(): Promise<void> {
    if (!isNativeDesktop() || this.pinning) return;
    this.pinning = true;
    this.pinError = '';
    try {
      this.taskbar = await nativeInvoke<NativeTaskbarPinStatus>('native_taskbar_pin_request');
      this.dismissTaskbarPrompt();
    } catch (caught) {
      this.pinError = errorMessage(
        caught,
        'Windows could not pin Kaede. Right-click its running taskbar icon and choose Pin to taskbar.'
      );
    } finally {
      this.pinning = false;
    }
  }
}

export const desktopLifecycle = new DesktopLifecycle();
